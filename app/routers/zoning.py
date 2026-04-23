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
    point = Point(lng, lat)  # GeoJSONは(lng, lat)の順
    for feature in geojson.get("features", []):
        try:
            geom = shape(feature["geometry"])
            if geom.contains(point):
                return feature
        except Exception:
            continue
    return None


@router.get("/v1/zoning")
async def get_zoning(
    lat: float = Query(..., description="緯度", example=35.6812),
    lng: float = Query(..., description="経度", example=139.7671),
):
    """緯度経度から用途地域・建蔽率・容積率・防火地域を返す"""

    if not REINFOLIB_API_KEY:
        raise HTTPException(status_code=500, detail="REINFOLIB_API_KEY not configured")

    x, y = latlng_to_tile(lat, lng, ZOOM)

    async with httpx.AsyncClient() as client:
        zoning_resp, fire_resp = await asyncio.gather(
            client.get(
                f"{BASE_URL}/XKT002",
                params={"response_format": "geojson", "z": ZOOM, "x": x, "y": y},
                headers=HEADERS,
                timeout=30,
            ),
            client.get(
                f"{BASE_URL}/XKT014",
                params={"response_format": "geojson", "z": ZOOM, "x": x, "y": y},
                headers=HEADERS,
                timeout=30,
            ),
        )

    result = {"lat": lat, "lng": lng}

    # 用途地域
    if zoning_resp.status_code == 200:
        feature = find_feature_at_point(zoning_resp.json(), lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["用途地域"] = props.get("use_area_ja", "")
            result["建蔽率"] = props.get("u_building_coverage_ratio_ja", "")
            result["容積率"] = props.get("u_floor_area_ratio_ja", "")
            result["市区町村"] = props.get("city_name", "")
            result["都道府県"] = props.get("prefecture", "")

    # 防火地域
    if fire_resp.status_code == 200:
        feature = find_feature_at_point(fire_resp.json(), lat, lng)
        if feature:
            props = feature.get("properties", {})
            result["防火地域"] = props.get("fire_prevention_ja", "")

    if "用途地域" not in result:
        result["用途地域"] = None
        result["message"] = "指定座標の用途地域データが見つかりませんでした"

    result["data_source"] = "不動産情報ライブラリ（国土交通省）"
    return result
