"""uvicorn 진입점: `uvicorn nightear.main:app`"""
import logging

from .app import create_app

# uvicorn 은 자기 로거만 설정한다. 앱 로그(INFO)도 docker logs 에 남도록 nightear 로거에 핸들러를 단다.
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger("nightear").addHandler(_handler)
logging.getLogger("nightear").setLevel(logging.INFO)

app = create_app()
