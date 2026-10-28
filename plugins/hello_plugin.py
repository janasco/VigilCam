"""Sample VigilCam plugin."""


def register(app):
    @app.get("/api/plugins/hello")
    async def hello_plugin():
        return {"plugin": "hello_plugin", "message": "Hello from VigilCam plugin!"}

    @app.get("/api/hello_plugin")
    async def hello_plugin_alias():
        return {"plugin": "hello_plugin", "message": "Hello from VigilCam plugin!"}
