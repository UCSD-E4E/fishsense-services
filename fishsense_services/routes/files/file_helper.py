from fishsense_database.database import HTTP_CODES, Database
from create_db import database


id_filters = ["all", "uploader", "photographer"]
file_filter = ["fish_species", "location", "time", "device", "device_class"]

def call_filter_helper(filter, request_info, sql_parameters):
    
    try:
    
        match(filter):
            case "fish_species":
                sql_parameters = fish_species_filter(request_info, sql_parameters)
                
            # case "location":
            #     sql_parameters = location_filter(request_info, sql_parameters)
                
            # case "time":
            #     sql_parameters["time"] = request_info["time"]
                
            # case "device":
            #     sql_parameters["device"] = request_info["device"]
                
            # case "device_class":
            #     sql_parameters["device_class"] = request_info["device_class"]
                
            case _:
                raise ValueError("Invalid file filter")
            
        return sql_parameters
            
    except ValueError as e:
        print(e)
        return {}
        
        
def fish_species_filter(request_info, sql_parameters):
    try:
        if "fish_species_name" not in request_info["fish_species"]:
            raise ValueError("Must provide a fish species name")
        
        parameters = {}
        parameters["fish_species_name"] = request_info["fish_species"]["fish_species_name"]
        
        with database as db:
            #script to get fish species id  
            species_id = db.exec_script("fishsense-database/scripts/get_scripts/get_fish_species_scripts/get_fish_species.sql", HTTP_CODES.GET.value, parameters)
        
        sql_parameters["species_id"] = species_id[0]
        
        return sql_parameters
    
    except ValueError as e:
        print(e)
        return {}

    
    