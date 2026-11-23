"""应用工厂。DB_PATH 环境变量指定 SQLite 文件位置，默认 <仓库>/data/closing.db。"""
import os

from flask import Flask


def create_app(config=None):
    app = Flask(__name__)
    default_db = os.path.join(app.root_path, "..", "data", "closing.db")
    app.config["DB_PATH"] = os.path.abspath(os.environ.get("DB_PATH", default_db))
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "loss-triangle-dev-key")
    if config:
        app.config.update(config)
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)

    from .db import close_db, init_db
    init_db(app)
    app.teardown_appcontext(close_db)

    from .views import bp
    app.register_blueprint(bp)
    return app
