# import json
# import re
# import datetime
# from fastapi import APIRouter, Request, HTTPException
# from fastapi.responses import JSONResponse
# from fastapi.encoders import jsonable_encoder

# from fishsense_database.database import HTTP_CODES, Database
# from create_db import database
# import fishsense_services.routes.files.file_helper as file_helper

# file_router = APIRouter()

# # structure for request query:
# # {
# #     "username": "username",
# #     "id_filter": "all/uploader/photographer",
# #     "file_filter": {
# #         "filter_name (check file_helper for existing filters)": {
# #             "filter_parameters (check sql name for proper key names)": "value" ...
# #        }
# #     }
# # }
# @file_router.get("/", status_code=200)
# async def get_files_filter(request : Request):
    
#     try:
#         data = await request.json()
#         request_info = json.loads(data)
#         file_filter = set()       
#         sql_parameters = {}
        
#         if "username" not in request_info:
#             raise ValueError("Must provide a username")
#         else:
#             with database as db:
#                 success = db.exec_script("fishsense-database/scripts/get_scripts/get_users_scripts/get_user_by_username.sql", HTTP_CODES.GET.value, data) 
        
#             if not success:
#                 raise ValueError("Error executing script")
            
#             sql_parameters["user_id"] = success[0]
        
#         if "id_filter" not in request_info:
#             id_filter = "all"
            
#         else:
#             id_filter = request_info["id_filter"]
            
#         if id_filter not in file_helper.id_filter:
#             raise ValueError("Invalid id filter")
        
#         if "file_filter" not in request_info:
#             file_filter.add("none")
            
#         else:
#             for filter in file_helper.file_filter: #TODO make sure filter type exists in filter list
#                 if filter not in file_helper.file_filter:
#                     raise ValueError("Invalid file filter or Incorrectly named filter")
#                 else:
#                     file_filter.add(filter)
#                     sql_parameters = file_helper.call_filter_helper(filter, request_info, sql_parameters)
#                     if len(sql_parameters) == 0:
#                         raise ValueError("Error in json key value pairs")
                    
#         sorted(file_filter)
        
#         sql_file = 'id='+id_filter+'&filter='
#         for filter in file_filter:
#             sql_file += filter + ','
#         sql_file = sql_file[:-1] #remove last comma
        
#         success = False
#         with database as db:
#             success = db.exec_script("fishsense-database/scripts/get_scripts/" + sql_file + ".sql", HTTP_CODES.GET.value, sql_parameters) 
        
#         if not success:
#             raise ValueError("Error executing script")
        
#         return JSONResponse(
#             status_code=200,
#             content={"files requested": jsonable_encoder(success)}
#         )
        
#     except ValueError as e:
#         return JSONResponse(
#             status_code=400,
#             content={"detail": str(e)}  # Send the error message as string
#         )   
        
# @file_router.post("/add_file", status_code=200)
# async def upload_file_route(request: Request):
    
#     data = await request.json()
    
#     try:
#         required_fields = ["upload_file", "mime_type", "captured_date_utc", "username", "photographer_username", "device_id", "deployment_id"]
#         for field in required_fields:
#             if field not in data or data[field] is None:
#                 raise ValueError(f"{field} cannot be null.")
            
        
#         sql_parameters = {}

#         # TODO store file and grab storage pool and file path
            
#         # TODO insert device id and deploy id
        
#         success = False
#         with database as db:
            
#             username_param = {"username": data["username"]}
#             user_id = db.exec_script("fishsense-database/scripts/get_scripts/get_users_scripts/get_user_by_username.sql", HTTP_CODES.POST, username_param)[0]

#             photographer_param = {"username": data["photographer_username"]}
#             photo_id = db.exec_script("fishsense-database/scripts/get_scripts/get_users_scripts/get_user_by_username.sql", HTTP_CODES.POST, photographer_param)
            
#             sql_parameters["uploader_id"] = user_id
            
            
#             if not photo_id:
#                 sql_parameters["photographer_id"] = None  
#             else:
#                 sql_parameters["photographer_id"] = photo_id
                
#             time_diff = db.calc_time_diff(datetime.datetime.now(datetime.timezone.utc))

#             sql_parameters["upload_date_utc"] = time_diff
            
#             date_regex = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
#             if not re.match(date_regex, data["captured_date_utc"]):
#                 raise ValueError("captured_date_utc is not in the correct format.")
            
#             captured_date_utc = datetime.datetime.strptime(data["captured_date_utc"], "%Y-%m-%d %H:%M:%S")
#             sql_parameters["captured_date_utc"] = db.calc_time_diff(captured_date_utc)
            
#             success = db.exec_script("fishsense-database/scripts/insert_scripts/insert_file.sql", HTTP_CODES.POST, sql_parameters) 

#         if not success:
#             raise ValueError("Error inserting file.")

#         return JSONResponse(
#             status_code=200,
#             content={"message": "User created successfully."}
#         )
    
#     except ValueError as e:
#         print(f"ValueError caught in FastAPI: {e}")  # Log the error for debugging
#         return JSONResponse(
#             status_code=400,
#             content={"detail": str(e)}  # Send the error message as string
#         )