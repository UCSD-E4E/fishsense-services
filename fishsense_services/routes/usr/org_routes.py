from fishsense_database.database import HTTP_CODES, Database
from create_db import database

import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

org_router = APIRouter()

@org_router.get("/org={organization_name}/users", status_code=200)
async def get_all_users_in_organization_route(organization_name: int):
    try:
        if not organization_name:
            raise ValueError("Must provide an organization name.")
        
        data = {"organization_id": organization_name}
        
        success = False
        with database as db:
            success = db.exec_script("fishsense-database/scripts/get_scripts/get_org_scripts/get_user_by_org.sql", HTTP_CODES.GET.value, data) 
        
        if not success:
            raise ValueError("Error executing script")
        
        return JSONResponse(
            status_code=200,
            content={"users": jsonable_encoder(success)}
        )
    
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"detail": str(e)}  # Send the error message as string
        )

@org_router.post("/create", status_code=201)
async def create_organization_route(request: Request):
    
    data = await request.json()
    
    try:
        required_fields = ["organization_name"]
        for field in required_fields:
            if field not in data or data[field] is None:
                raise ValueError(f"{field} cannot be null.")
                
        success = False
        with database as db:
            success = db.exec_script("fishsense-database/scripts/insert_scripts/insert_organization.sql", HTTP_CODES.POST.value, data) 
        
        if not success:
            raise ValueError("Error executing script")
        
        return JSONResponse(
            status_code=201,
            content={"message": "Organization created successfully"}
        )
            
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"detail": str(e)}  # Send the error message as string
        )
        
@org_router.get("/org={organization_name}", status_code=200)
async def get_organization_route(organization_name: str):
    
    try:
        if not organization_name:
            raise ValueError("Must provide an organization name.")
        
        data = {"organization_name": organization_name}
        
        success = False
        with database as db:
            success = db.exec_script("fishsense-database/scripts/get_scripts/get_org_scripts/get_org_by_name.sql", HTTP_CODES.GET.value, data) 
        
        if not success:
            raise ValueError("Error executing script")
        
        return JSONResponse(
            status_code=200,
            content={"organization": jsonable_encoder(success)}
        )
    
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"detail": str(e)}  # Send the error message as string
        )
@org_router.get("/", status_code=200)
async def get_all_orgs_route():

    try:
        success = False
        with database as db:
            success = db.exec_script("fishsense-database/scripts/get_scripts/get_org_scripts/get_all_orgs.sql", http_code=HTTP_CODES.GET.value) 
        
        if not success:
            raise ValueError("Error executing script")
        
        return JSONResponse(
            status_code=200,
            content={"organizations": jsonable_encoder(success)}
        )
    
    except ValueError as e:
        
        return JSONResponse(
            status_code=400,
            content={"detail": str(e)}  # Send the error message as string
        )


@org_router.post("/add_user", status_code=200)
async def add_user_to_organization(request: Request):
    
    data = await request.json()
    
    try:
        required_fields = ["organization_name", "username"]
        for field in required_fields:
            if field not in data or data[field] is None:
                raise ValueError(f"{field} cannot be null.")
        
        success = False
        with database as db:
            user_id = db.exec_script("fishsense-database/scripts/get_scripts/get_users_scripts/get_user_by_username.sql", HTTP_CODES.GET.value, data)[0][0]
            org_id = db.exec_script("fishsense-database/scripts/get_scripts/get_org_scripts/get_org_by_name.sql", HTTP_CODES.GET.value, data)[0][0]
            
            user_id = user_id[0]
            org_id = org_id[0]
            
            parameters = {"user_id": user_id, "org_id": org_id}
            
            success = db.exec_script("fishsense-database/scripts/insert_scripts/insert_user_organization.sql", HTTP_CODES.POST.value, parameters) 
        
        if not success:
            raise ValueError("Error executing script")
        
        return JSONResponse(
            status_code=200,
            content={"message": "User added to organization successfully"}
        )
            
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"detail": str(e)}  # Send the error message as string
        )
    
