"""uvicorn 진입점: `uvicorn nightear.main:app`"""
from .app import create_app

app = create_app()
