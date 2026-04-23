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


async def fetch_tile(client: httpx.AsyncClient, endpoint: str, z: int, x: int, y: int) -> httpx.Response:
    return await client.get(
        f"{BASE_URL}/{endpoint}",
        params={"response_format": "geojson", "z": z, "x": x, "y": y},
        headers=HEADERS,
        timeout=30,
    )


@router.get("/v1/zoning")
async def get_zoning(
    lat: float = Query(..., description="緯度", example=35.6812),
    lng: float = Query(..., description="経度", example=139.7671),
):
    """緯度経度から建築規制情報・防災情報を一括取得する"""

    if not REINFOLIB_API_KEY:
        raise HTTPException(status_code=500, detail="REINFOLIB_API_KEY not configured")

    x, y = latlng_to_tile(lat, lng, ZOOM)

    async with httpx.AsyncClient() as client:
        responses = await asyncio.gather(
            fetch_tile(client, "XKT001", ZOOM, x, y),  # 都市計画区域
            fetch_tile(client, "XKT002", ZOOM, x, y),  # 用途地域
            fetch_tile(client, "XKT014", ZOOM, x, y),  # 防火地域
            fetch_tile(client, "XKT023", ZOOM, x, y),  # 地区計画
            fetch_tile(client, "XKT024", ZOOM, x, y),  # 高度利用地区
            fetch_tile(client, "XKT026", ZOOM, x, y),  # 洪水浸水想定区域
            fetch_tile(client, "XKT029", ZOOM, x, y),  # 土砂災害警戒区域
        )
        area_resp, zoning_resp, fire_resp, district_resp, height_resp, flood_resp, landslide_resp = responses

    result = {"lat": lat, "lng": lng}

    # 都市計画区域（XKT001）
    if area_resp.status_code == 200:
        feature = find_feature_at_point(area_resp.json(), lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["都市計画区域"] = props.get("area_classification_ja", "")

    # 用途地域（XKT002）
    if zoning_resp.status_code == 200:
        feature = find_feature_at_point(zoning_resp.json(), lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["用途地域"] = props.get("use_area_ja", "")
            result["建蔽率"] = props.get("u_building_coverage_ratio_ja", "")
            result["容積率"] = props.get("u_floor_area_ratio_ja", "")
            result["市区町村"] = props.get("city_name", "")
            result["都道府県"] = props.get("prefecture", "")

    # 防火地域（XKT014）
    if fire_resp.status_code == 200:
        feature = find_feature_at_point(fire_resp.json(), lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["防火地域"] = props.get("fire_prevention_ja", "")

    # 地区計画（XKT023）
    if district_resp.status_code == 200:
        feature = find_feature_at_point(district_resp.json(), lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["地区計画"] = props.get("plan_name", "")

    # 高度利用地区（XKT024）
    if height_resp.status_code == 200:
        feature = find_feature_at_point(height_resp.json(), lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["高度利用地区"] = props.get("plan_name", props.get("name", ""))

    # 洪水浸水想定区域（XKT026）
    if flood_resp.status_code == 200:
        features = find_all_features_at_point(flood_resp.json(), lat, lng)
        if features:
            result["洪水浸水想定"] = [
                {
                    "浸水深": f.get("properties", {}).get("A31a_205", ""),
                    "河川名": f.get("properties", {}).get("A31a_202", ""),
                }
                for f in features
            ]

    # 土砂災害警戒区域（XKT029）
    if landslide_resp.status_code == 200:
        features = find_all_features_at_point(landslide_resp.json(), lat, lng)
        if features:
            result["土砂災害警戒区域"] = [
                f.get("properties", {})
                for f in features
            ]

    if "用途地域" not in result:
        result["用途地域"] = None
        result["message"] = "指定座標の用途地域データが見つかりませんでした"

    result["data_source"] = "不動産情報ライブラリ（国土交通省）"
    return result
