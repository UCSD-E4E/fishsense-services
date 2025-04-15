from fishsense_database.create_db import database 
import datetime

def get_user_by_email(query_params : str):
    try:
        with database as db:
            user = db.exec_script("fishsense-database/fishsense_database/scripts/get_scripts/get_users_scripts/get_user.sql", query_params)
            
            return user
    
    except Exception as e:
        print("Error fetching user by email:", e)
        return None
    
def update_last_login(query_params : str):
    
    try:
        time = unix_time_calc(datetime.now(datetime.timezone.utc))
        query_params['last_login'] = time
        
        with database as db:
            user = db.exec_script("fishsense-database/fishsense_database/scripts/update_scripts/update_user_scripts/update_last_login.sql", query_params)
            
            return user
        
    except Exception as e:
        print("Error updating last login:", e)
        return None
    
def unix_time_calc(date : datetime):
    
    unix_time = int(date.timestamp())
    return unix_time