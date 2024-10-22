from fastapi import FastAPI
from fishsense_services.routes.usr.login import login_router 


def create_app():
    app = FastAPI()
    
    app.include_router(login_router, prefix="/login")

    return app

app = create_app()


