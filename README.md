git clone with --recurse-submodules
remeber to git submodule update!!

git submodule update --remote --recursive --force

whenever you edit fishsense-database
git submodule update --remote --recursive --force

cd into fishsense-database in run
docker exec -it postgres psql -d fishsense_db -U placeholder_superuser
to test database

git submodule init
git submodule update