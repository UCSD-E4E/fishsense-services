from fastapi import FastAPI
from fishsense_services.routes.usr.login_routes import login_router 
from fishsense_services.routes.usr.user_routes import usr_router


def create_app():
    app = FastAPI()
    
    app.include_router(login_router, prefix="/login")
    app.include_router(usr_router, prefix="/usr")

    return app

app = create_app()


