"""
myAISky 진입점 (하위 호환용).

실제 코드는 backend/main.py (API) 와 frontend/ (대시보드 UI) 에 있습니다.
    uvicorn app:app --reload --host 127.0.0.1 --port 8000
    또는
    uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
"""

from backend.main import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
