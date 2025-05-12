from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from fishsense_services.routes.usr.login import login_router 
from fishsense_services.routes.processing.laser_cal import laser_router 
from fishsense_services.routes.processing.fish import fish_router 


# from fishsense_services.routes.usr.usr import usr_router
# from fishsense_services.routes.img.img import img_router

def create_app():
    
    app = FastAPI()
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        
    )
    
    app.include_router(login_router, prefix="/login")
    app.include_router(laser_router, prefix="/laser")
    app.include_router(fish_router, prefix="/fish")

    # app.include_router(usr_router, prefix="/usr")
    # app.include_router(img_router, prefix="/img")

    return app

app = create_app()



