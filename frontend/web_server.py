"""旧启动命令兼容入口；与 backend.app.main 使用同一个应用，无反向代理。"""
from backend.app.main import app
