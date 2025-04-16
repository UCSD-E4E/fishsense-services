git clone with --recurse-submodules
remeber to git submodule update!!

# Initialize submodules
git submodule init

# Update and force fetch all branches
git submodule update --remote --recursive --force

# How set up google oauth


# Checking last log in time works
1. docker exec -it postgres psql -U placeholder_superuser -d fishsense_db
2. SELECT email, last_login_utc FROM users WHERE email = 'your email from google oauth setup';
3. SELECT email, jwt_token FROM users WHERE email = 'your email from google oauth setup';
