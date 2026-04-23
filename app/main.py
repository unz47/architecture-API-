from fastapi import FastAPI
from app.routers import zoning

app = FastAPI(
    title="建築確認API",
    description="緯度経度から用途地域・建蔽率・容積率・防火地域を返すAPI",
    version="0.1.0",
)

app.include_router(zoning.router)
