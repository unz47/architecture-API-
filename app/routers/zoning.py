import asyncio
import math
import re

import httpx
from fastapi import APIRouter, Query, HTTPException
from shapely.geometry import Point, shape

from app.config import REINFOLIB_API_KEY

router = APIRouter()

BASE_URL = "https://www.reinfolib.mlit.go.jp/ex-api/external"
HEADERS = {
    "Ocp-Apim-Subscription-Key": REINFOLIB_API_KEY,
}
ZOOM = 15


# ── ユーティリティ ──────────────────────────────────


def latlng_to_tile(lat: float, lng: float, zoom: int) -> tuple[int, int]:
    """緯度経度をXYZタイル座標に変換する"""
    n = 2 ** zoom
    x = int((lng + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def find_feature_at_point(geojson: dict, lat: float, lng: float) -> dict | None:
    """GeoJSONから指定座標を含むポリゴンのFeatureを返す"""
    point = Point(lng, lat)
    for feature in geojson.get("features", []):
        try:
            geom = shape(feature["geometry"])
            if geom.contains(point):
                return feature
        except Exception:
            continue
    return None


def find_all_features_at_point(geojson: dict, lat: float, lng: float) -> list[dict]:
    """GeoJSONから指定座標を含む全てのFeatureを返す"""
    point = Point(lng, lat)
    results = []
    for feature in geojson.get("features", []):
        try:
            geom = shape(feature["geometry"])
            if geom.contains(point):
                results.append(feature)
        except Exception:
            continue
    return results


def find_nearby_features(
    geojson: dict, lat: float, lng: float, limit: int = 5
) -> list[dict]:
    """GeoJSONから指定座標に近い順にFeatureを返す（ポイントデータ用）"""
    point = Point(lng, lat)
    features_with_dist = []
    for feature in geojson.get("features", []):
        try:
            geom = shape(feature["geometry"])
            dist = point.distance(geom)
            features_with_dist.append((dist, feature))
        except Exception:
            continue
    features_with_dist.sort(key=lambda x: x[0])
    return [f for _, f in features_with_dist[:limit]]


async def fetch_tile(
    client: httpx.AsyncClient, endpoint: str, z: int, x: int, y: int
) -> httpx.Response:
    """REINFOLIBタイルAPIを呼び出す"""
    return await client.get(
        f"{BASE_URL}/{endpoint}",
        params={"response_format": "geojson", "z": z, "x": x, "y": y},
        headers=HEADERS,
        timeout=30,
    )


def safe_json(resp: httpx.Response) -> dict | None:
    """レスポンスが200でJSONパース可能な場合のみdictを返す"""
    if resp.status_code == 200:
        try:
            return resp.json()
        except Exception:
            return None
    return None


def parse_percent(value: str | None) -> float | None:
    """'80%' のような文字列からfloatを取り出す"""
    if not value:
        return None
    m = re.search(r"([\d.]+)", str(value))
    return float(m.group(1)) if m else None


# ── 計算ロジック ──────────────────────────────────


def calc_disaster_risk_score(result: dict) -> dict:
    """防災データから災害リスクスコア(0-100)を算出する"""
    score = 0
    details = []

    # 洪水浸水（最大30点）
    flood = result.get("洪水浸水想定")
    if flood:
        max_depth = 0
        for f in flood:
            depth = f.get("浸水深")
            if isinstance(depth, (int, float)):
                max_depth = max(max_depth, depth)
        if max_depth >= 5:
            pts = 30
        elif max_depth >= 3:
            pts = 25
        elif max_depth >= 1:
            pts = 20
        elif max_depth >= 0.5:
            pts = 10
        else:
            pts = 5
        score += pts
        details.append(f"洪水浸水(浸水深{max_depth}): +{pts}")

    # 津波（最大25点）
    if result.get("津波浸水想定"):
        score += 25
        details.append("津波浸水想定区域内: +25")

    # 土砂災害（最大20点）
    sediment = result.get("土砂災害警戒区域")
    if sediment:
        score += 20
        details.append("土砂災害警戒区域内: +20")

    # 液状化（最大15点）
    liq = result.get("液状化")
    if liq:
        level = liq.get("liquefaction_tendency_level")
        if level == 1:
            pts = 15
        elif level == 2:
            pts = 10
        elif level == 3:
            pts = 5
        else:
            pts = 0
        if pts > 0:
            score += pts
            note = liq.get("note", "")
            details.append(f"液状化({note}): +{pts}")

    # 高潮（最大10点）
    if result.get("高潮浸水想定"):
        score += 10
        details.append("高潮浸水想定区域内: +10")

    # 災害危険区域（+10点）
    if result.get("災害危険区域"):
        score += 10
        details.append("災害危険区域内: +10")

    # 地すべり防止地区（+10点）
    if result.get("地すべり防止地区"):
        score += 10
        details.append("地すべり防止地区内: +10")

    # 急傾斜地崩壊危険区域（+10点）
    if result.get("急傾斜地崩壊危険区域"):
        score += 10
        details.append("急傾斜地崩壊危険区域内: +10")

    score = min(score, 100)

    if score == 0:
        level = "低"
    elif score <= 20:
        level = "やや低"
    elif score <= 40:
        level = "中"
    elif score <= 60:
        level = "やや高"
    elif score <= 80:
        level = "高"
    else:
        level = "非常に高"

    return {
        "スコア": score,
        "リスクレベル": level,
        "内訳": details,
    }


def calc_building_volume(result: dict, site_area: float) -> dict | None:
    """用途地域+建蔽率+容積率から建築可能ボリュームを概算する"""
    bcr = parse_percent(result.get("建蔽率"))
    far = parse_percent(result.get("容積率"))

    if bcr is None or far is None:
        return None

    building_area = site_area * bcr / 100
    total_floor = site_area * far / 100
    max_floors = int(total_floor / building_area) if building_area > 0 else 0

    return {
        "敷地面積_m2": site_area,
        "建蔽率": f"{bcr}%",
        "容積率": f"{far}%",
        "建築面積_m2": round(building_area, 2),
        "最大延床面積_m2": round(total_floor, 2),
        "想定階数": max_floors,
        "注記": "斜線制限・高さ制限・日影規制は未考慮の概算値です",
    }


def calc_area_future_score(result: dict) -> dict | None:
    """将来推計人口から20年後のエリア将来性を算出する"""
    pop = result.get("将来推計人口")
    if not pop:
        return None

    # PTN_YYYY = 総人口推計（250mメッシュ内の人数）
    current = pop.get("PTN_2025") or pop.get("PTN_2020")
    future_20y = pop.get("PTN_2045")
    future_30y = pop.get("PTN_2050")

    if current is None or current == 0:
        return None

    result_data = {"現在推計人口": round(current, 1)}

    if future_20y is not None:
        change_20 = (future_20y - current) / current * 100
        result_data["20年後推計人口"] = round(future_20y, 1)
        result_data["20年後増減率"] = f"{change_20:+.1f}%"

    if future_30y is not None:
        change_30 = (future_30y - current) / current * 100
        result_data["25年後推計人口"] = round(future_30y, 1)
        result_data["25年後増減率"] = f"{change_30:+.1f}%"

    # スコアリング: 人口増加→高スコア、減少→低スコア
    if future_20y is not None:
        change = (future_20y - current) / current * 100
        if change >= 10:
            score, level = 90, "非常に高い"
        elif change >= 0:
            score, level = 70, "高い"
        elif change >= -10:
            score, level = 50, "普通"
        elif change >= -20:
            score, level = 30, "やや低い"
        else:
            score, level = 10, "低い"
        result_data["将来性スコア"] = score
        result_data["評価"] = level

    return result_data


def build_regulation_summary(result: dict) -> str:
    """法規情報を自然言語で1段落にまとめる"""
    parts = []

    # 所在
    pref = result.get("都道府県", "")
    city = result.get("市区町村", "")
    if pref and city:
        parts.append(f"所在地は{pref}{city}")

    # 都市計画
    area = result.get("都市計画区域")
    if area:
        parts.append(f"{area}内")

    # 用途地域
    zone = result.get("用途地域")
    if zone:
        bcr = result.get("建蔽率", "")
        far = result.get("容積率", "")
        parts.append(f"用途地域は{zone}（建蔽率{bcr}・容積率{far}）")

    # 防火
    fire = result.get("防火地域")
    if fire:
        parts.append(f"{fire}に指定")

    # 地区計画
    dp = result.get("地区計画")
    if dp:
        parts.append(f"地区計画「{dp}」の区域内")

    # 都市計画道路
    if result.get("都市計画道路"):
        parts.append("都市計画道路がかかる可能性あり")

    # 立地適正化
    if result.get("立地適正化計画"):
        parts.append("立地適正化計画の区域内")

    # 災害リスク
    risks = []
    if result.get("洪水浸水想定"):
        rivers = [f.get("河川名", "") for f in result["洪水浸水想定"] if f.get("河川名")]
        if rivers:
            risks.append(f"洪水浸水想定（{', '.join(rivers)}）")
        else:
            risks.append("洪水浸水想定区域")
    if result.get("津波浸水想定"):
        risks.append("津波浸水想定区域")
    if result.get("高潮浸水想定"):
        risks.append("高潮浸水想定区域")
    if result.get("土砂災害警戒区域"):
        risks.append("土砂災害警戒区域")
    if result.get("地すべり防止地区"):
        risks.append("地すべり防止地区")
    if result.get("急傾斜地崩壊危険区域"):
        risks.append("急傾斜地崩壊危険区域")

    liq = result.get("液状化")
    if liq:
        note = liq.get("note", "")
        if note:
            risks.append(f"液状化（{note}）")

    if risks:
        parts.append("災害リスクとして" + "・".join(risks) + "に該当")
    else:
        parts.append("主要な災害リスク区域には非該当")

    return "。".join(parts) + "。"


# ── エンドポイント ──────────────────────────────────


@router.get("/v1/zoning")
async def get_zoning(
    lat: float = Query(..., description="緯度", example=35.6812),
    lng: float = Query(..., description="経度", example=139.7671),
    site_area: float | None = Query(
        None, description="敷地面積（m2）。指定すると建築可能ボリュームを概算", example=200
    ),
):
    """緯度経度から建築規制情報・防災情報・マーケット情報を一括取得する"""

    if not REINFOLIB_API_KEY:
        raise HTTPException(status_code=500, detail="REINFOLIB_API_KEY not configured")

    x, y = latlng_to_tile(lat, lng, ZOOM)

    async with httpx.AsyncClient() as client:
        responses = await asyncio.gather(
            # ── 都市計画系 ──
            fetch_tile(client, "XKT001", ZOOM, x, y),  # 0: 都市計画区域
            fetch_tile(client, "XKT002", ZOOM, x, y),  # 1: 用途地域
            fetch_tile(client, "XKT003", ZOOM, x, y),  # 2: 立地適正化計画
            fetch_tile(client, "XKT014", ZOOM, x, y),  # 3: 防火地域
            fetch_tile(client, "XKT023", ZOOM, x, y),  # 4: 地区計画
            fetch_tile(client, "XKT024", ZOOM, x, y),  # 5: 高度利用地区
            fetch_tile(client, "XKT030", ZOOM, x, y),  # 6: 都市計画道路
            # ── 防災系 ──
            fetch_tile(client, "XKT016", ZOOM, x, y),  # 7: 災害危険区域
            fetch_tile(client, "XKT021", ZOOM, x, y),  # 8: 地すべり防止地区
            fetch_tile(client, "XKT022", ZOOM, x, y),  # 9: 急傾斜地崩壊危険区域
            fetch_tile(client, "XKT025", ZOOM, x, y),  # 10: 液状化
            fetch_tile(client, "XKT026", ZOOM, x, y),  # 11: 洪水浸水想定区域
            fetch_tile(client, "XKT027", ZOOM, x, y),  # 12: 高潮浸水想定区域
            fetch_tile(client, "XKT028", ZOOM, x, y),  # 13: 津波浸水想定
            fetch_tile(client, "XKT029", ZOOM, x, y),  # 14: 土砂災害警戒区域
            # ── 周辺・マーケット ──
            fetch_tile(client, "XGT001", ZOOM, x, y),  # 15: 指定緊急避難場所
            fetch_tile(client, "XPT002", ZOOM, x, y),  # 16: 地価公示・地価調査
            fetch_tile(client, "XKT013", ZOOM, x, y),  # 17: 将来推計人口
            fetch_tile(client, "XKT004", ZOOM, x, y),  # 18: 小学校区
            fetch_tile(client, "XKT015", ZOOM, x, y),  # 19: 駅別乗降客数
            fetch_tile(client, "XKT031", ZOOM, x, y),  # 20: 人口集中地区
        )

    result = {"lat": lat, "lng": lng}

    # ━━ 都市計画系 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 都市計画区域（XKT001）
    data = safe_json(responses[0])
    if data:
        feature = find_feature_at_point(data, lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["都市計画区域"] = props.get("area_classification_ja", "")

    # 用途地域（XKT002）
    data = safe_json(responses[1])
    if data:
        feature = find_feature_at_point(data, lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["用途地域"] = props.get("use_area_ja", "")
            result["建蔽率"] = props.get("u_building_coverage_ratio_ja", "")
            result["容積率"] = props.get("u_floor_area_ratio_ja", "")
            result["市区町村"] = props.get("city_name", "")
            result["都道府県"] = props.get("prefecture", "")

    # 立地適正化計画（XKT003）
    data = safe_json(responses[2])
    if data:
        features = find_all_features_at_point(data, lat, lng)
        if features:
            result["立地適正化計画"] = [
                f.get("properties", {}) for f in features
            ]

    # 防火地域（XKT014）
    data = safe_json(responses[3])
    if data:
        feature = find_feature_at_point(data, lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["防火地域"] = props.get("fire_prevention_ja", "")

    # 地区計画（XKT023）
    data = safe_json(responses[4])
    if data:
        feature = find_feature_at_point(data, lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["地区計画"] = props.get("plan_name", "")

    # 高度利用地区（XKT024）
    data = safe_json(responses[5])
    if data:
        feature = find_feature_at_point(data, lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["高度利用地区"] = props.get("plan_name", props.get("name", ""))

    # 都市計画道路（XKT030）
    data = safe_json(responses[6])
    if data:
        features = find_all_features_at_point(data, lat, lng)
        if features:
            result["都市計画道路"] = [
                f.get("properties", {}) for f in features
            ]

    # ━━ 防災系 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # 災害危険区域（XKT016）
    data = safe_json(responses[7])
    if data:
        features = find_all_features_at_point(data, lat, lng)
        if features:
            result["災害危険区域"] = [
                f.get("properties", {}) for f in features
            ]

    # 地すべり防止地区（XKT021）
    data = safe_json(responses[8])
    if data:
        features = find_all_features_at_point(data, lat, lng)
        if features:
            result["地すべり防止地区"] = [
                f.get("properties", {}) for f in features
            ]

    # 急傾斜地崩壊危険区域（XKT022）
    data = safe_json(responses[9])
    if data:
        features = find_all_features_at_point(data, lat, lng)
        if features:
            result["急傾斜地崩壊危険区域"] = [
                f.get("properties", {}) for f in features
            ]

    # 液状化の発生傾向（XKT025）
    data = safe_json(responses[10])
    if data:
        feature = find_feature_at_point(data, lat, lng)
        if feature:
            result["液状化"] = feature.get("properties", {})

    # 洪水浸水想定区域（XKT026）
    data = safe_json(responses[11])
    if data:
        features = find_all_features_at_point(data, lat, lng)
        if features:
            result["洪水浸水想定"] = [
                {
                    "浸水深": f.get("properties", {}).get("A31a_205", ""),
                    "河川名": f.get("properties", {}).get("A31a_202", ""),
                }
                for f in features
            ]

    # 高潮浸水想定区域（XKT027）
    data = safe_json(responses[12])
    if data:
        features = find_all_features_at_point(data, lat, lng)
        if features:
            result["高潮浸水想定"] = [
                f.get("properties", {}) for f in features
            ]

    # 津波浸水想定（XKT028）
    data = safe_json(responses[13])
    if data:
        features = find_all_features_at_point(data, lat, lng)
        if features:
            result["津波浸水想定"] = [
                f.get("properties", {}) for f in features
            ]

    # 土砂災害警戒区域（XKT029）
    data = safe_json(responses[14])
    if data:
        features = find_all_features_at_point(data, lat, lng)
        if features:
            result["土砂災害警戒区域"] = [
                f.get("properties", {}) for f in features
            ]

    # ━━ 周辺情報・マーケット ━━━━━━━━━━━━━━━━━━━━━━━━━

    # 指定緊急避難場所（XGT001）- ポイントデータなので近傍検索
    data = safe_json(responses[15])
    if data:
        nearby = find_nearby_features(data, lat, lng, limit=5)
        if nearby:
            result["指定緊急避難場所"] = [
                f.get("properties", {}) for f in nearby
            ]

    # 地価公示・地価調査（XPT002）- ポイントデータなので近傍検索
    data = safe_json(responses[16])
    if data:
        nearby = find_nearby_features(data, lat, lng, limit=5)
        if nearby:
            result["地価公示"] = [
                f.get("properties", {}) for f in nearby
            ]

    # 将来推計人口（XKT013）- メッシュデータ
    data = safe_json(responses[17])
    if data:
        feature = find_feature_at_point(data, lat, lng)
        if feature:
            result["将来推計人口"] = feature.get("properties", {})

    # 小学校区（XKT004）
    data = safe_json(responses[18])
    if data:
        feature = find_feature_at_point(data, lat, lng)
        if feature:
            result["小学校区"] = feature.get("properties", {})

    # 駅別乗降客数（XKT015）- ポイントデータなので近傍検索
    data = safe_json(responses[19])
    if data:
        nearby = find_nearby_features(data, lat, lng, limit=3)
        if nearby:
            result["最寄り駅"] = [
                f.get("properties", {}) for f in nearby
            ]

    # 人口集中地区（XKT031）
    data = safe_json(responses[20])
    if data:
        feature = find_feature_at_point(data, lat, lng)
        if feature:
            result["人口集中地区"] = feature.get("properties", {})

    # ━━ 計算ロジック（付加価値） ━━━━━━━━━━━━━━━━━━━━━━

    # 災害リスクスコア
    result["災害リスクスコア"] = calc_disaster_risk_score(result)

    # 建築可能ボリューム概算（敷地面積が指定された場合のみ）
    if site_area is not None and site_area > 0:
        volume = calc_building_volume(result, site_area)
        if volume:
            result["建築可能ボリューム概算"] = volume

    # エリア将来性
    future = calc_area_future_score(result)
    if future:
        result["エリア将来性"] = future

    # 法規サマリー
    result["法規サマリー"] = build_regulation_summary(result)

    # ━━ フォールバック ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    if "用途地域" not in result:
        result["用途地域"] = None
        result["message"] = "指定座標の用途地域データが見つかりませんでした"

    result["data_source"] = "不動産情報ライブラリ（国土交通省）"
    return result
