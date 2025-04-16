git clone with --recurse-submodules
remeber to git submodule update!!

# Initialize submodules
git submodule init

# Update and force fetch all branches
git submodule update --remote --recursive --force

# How set up google api
1. https://console.cloud.google.com/

2. Create a Project

3. In the left sidebar, go to “APIs & Services” > “Library”

Search for Oauth and enable it.

4. Set Up Credentials

Go to “APIs & Services” > “Credentials”

Choose OAuth 2.0 Client ID 

5. Go to "OAuth consent screen"

Choose "External", fill out info

6. Add Redirect URIs (for OAuth login)

7. Copy Your Credentials: Client ID and Secret, or API Key



# Checking last log in time works
1. docker exec -it postgres psql -U placeholder_superuser -d fishsense_db
2. SELECT email, last_login_utc FROM users WHERE email = 'your email from google oauth setup';
3. SELECT email, jwt_token FROM users WHERE email = 'your email from google oauth setup';
