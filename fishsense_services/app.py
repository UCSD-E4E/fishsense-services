from fastapi import FastAPI
from fishsense_services.routes.usr.login_routes import login_router 
from fishsense_services.routes.usr.user_routes import usr_router
from fishsense_services.routes.usr.org_routes import org_router
from fishsense_services.routes.files.file_routes import file_router


def create_app():
    app = FastAPI()
    
    app.include_router(login_router, prefix="/login")
    app.include_router(usr_router, prefix="/usr")
    app.include_router(org_router, prefix="/org")
    app.include_router(file_router, prefix="/files")
    

    return app

app = create_app()


