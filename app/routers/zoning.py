import asyncio
import math

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


# ── エンドポイント ──────────────────────────────────


@router.get("/v1/zoning")
async def get_zoning(
    lat: float = Query(..., description="緯度", example=35.6812),
    lng: float = Query(..., description="経度", example=139.7671),
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
            # ── その他 ──
            fetch_tile(client, "XGT001", ZOOM, x, y),  # 15: 指定緊急避難場所
            fetch_tile(client, "XPT002", ZOOM, x, y),  # 16: 地価公示・地価調査
            fetch_tile(client, "XKT013", ZOOM, x, y),  # 17: 将来推計人口
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

    # ━━ その他（周辺情報・マーケット） ━━━━━━━━━━━━━━━━━━

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

    # ━━ フォールバック ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    if "用途地域" not in result:
        result["用途地域"] = None
        result["message"] = "指定座標の用途地域データが見つかりませんでした"

    result["data_source"] = "不動産情報ライブラリ（国土交通省）"
    return result
