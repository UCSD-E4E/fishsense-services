from fastapi import FastAPI
from fishsense_services.routes.usr.login import login_router 
from fishsense_services.routes.usr.usr import usr_router
from fishsense_services.routes.img.img import img_router


def create_app():
    app = FastAPI()
    
    app.include_router(login_router, prefix="/login")
    app.include_router(usr_router, prefix="/usr")
    app.include_router(img_router, prefix="/img")

    return app

app = create_app()


