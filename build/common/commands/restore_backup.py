import os
import datetime
import tarfile
import hashlib
import frappe
import boto3

from push_backup import DATE_FORMAT, check_environment_variables
from frappe.utils import get_sites, random_string
from frappe.commands.site import _new_site
from frappe.installer import make_conf, get_conf_params, make_site_dirs
from check_connection import get_site_config, get_config

def list_directories(path):
    directories = []
    for name in os.listdir(path):
        if os.path.isdir(os.path.join(path, name)):
            directories.append(name)
    return directories

def get_backup_dir():
    return os.path.join(
        os.path.expanduser('~'),
        'backups'
    )

def decompress_db(files_base, site):
    database_file = files_base + '-database.sql.gz'
    config = get_config()
    site_config = get_site_config(site)
    db_root_user = os.environ.get('DB_ROOT_USER', 'root')
    command = 'gunzip -c {database_file} > {database_extract}'.format(
        database_file=database_file,
        database_extract=database_file.replace('.gz','')
    )

    print('Extract Database GZip for site {}'.format(site))
    os.system(command)

def restore_database(files_base, site):
    db_root_password = os.environ.get('MYSQL_ROOT_PASSWORD')
    if not db_root_password:
        print('Variable MYSQL_ROOT_PASSWORD not set')
        exit(1)

    db_root_user = os.environ.get("DB_ROOT_USER", 'root')

    # restore database
    database_file = files_base + '-database.sql.gz'
    decompress_db(files_base, site)
    config = get_config()
    site_config = get_site_config(site)

    # mysql command prefix
    mysql_command = 'mysql -u{db_root_user} -h{db_host} -p{db_password} -e '.format(
        db_root_user=db_root_user,
        db_host=config.get('db_host'),
        db_password=db_root_password
    )

    # drop db if exists for clean restore
    drop_database = mysql_command + "\"DROP DATABASE IF EXISTS \`{db_name}\`;\"".format(
        db_name=site_config.get('db_name')
    )
    os.system(drop_database)

    # create db
    create_database = mysql_command + "\"CREATE DATABASE IF NOT EXISTS \`{db_name}\`;\"".format(
        db_name=site_config.get('db_name')
    )
    os.system(create_database)

    # create user
    create_user = mysql_command + "\"CREATE USER IF NOT EXISTS \'{db_name}\'@\'%\' IDENTIFIED BY \'{db_password}\'; FLUSH PRIVILEGES;\"".format(
        db_name=site_config.get('db_name'),
        db_password=site_config.get('db_password')
    )
    os.system(create_user)

    # create user password
    set_user_password = mysql_command + "\"UPDATE mysql.user SET authentication_string = PASSWORD('{db_password}') WHERE User = \'{db_name}\' AND Host = \'%\';\"".format(
        db_name=site_config.get('db_name'),
        db_password=site_config.get('db_password')
    )
    os.system(set_user_password)

    # grant db privileges to user
    grant_privileges = mysql_command + "\"GRANT ALL PRIVILEGES ON \`{db_name}\`.* TO '{db_name}'@'%'; FLUSH PRIVILEGES;\"".format(
        db_name=site_config.get('db_name')
    )
    os.system(grant_privileges)

    command = "mysql -u{db_root_user} -h{db_host} -p{db_password} '{db_name}' < {database_file}".format(
        db_root_user=db_root_user,
        db_host=config.get('db_host'),
        db_password=db_root_password,
        db_name=site_config.get('db_name'),
        database_file=database_file.replace('.gz',''),
    )

    print('Restoring database for site: {}'.format(site))
    os.system(command)

def restore_files(files_base):
    public_files = files_base + '-files.tar'
    # extract tar
    public_tar = tarfile.open(public_files)
    print('Extracting {}'.format(public_files))
    public_tar.extractall()

def restore_private_files(files_base):
    private_files = files_base + '-private-files.tar'
    private_tar = tarfile.open(private_files)
    print('Extracting {}'.format(private_files))
    private_tar.extractall()

def pull_backup_from_s3():
    check_environment_variables()

    # https://stackoverflow.com/a/54672690
    s3 = boto3.resource(
        's3',
        aws_access_key_id=os.environ.get('ACCESS_KEY_ID'),
        aws_secret_access_key=os.environ.get('SECRET_ACCESS_KEY'),
        endpoint_url=os.environ.get('ENDPOINT_URL')
    )

    bucket_dir = os.environ.get('BUCKET_DIR')
    bucket_name = os.environ.get('BUCKET_NAME')
    bucket = s3.Bucket(bucket_name)

    # Change directory to /home/frappe/backups
    os.chdir(get_backup_dir())

    for obj in bucket.objects.filter(Prefix = bucket_dir):
        backup_file = obj.key.replace(os.path.join(bucket_dir,''),'')
        if not os.path.exists(os.path.dirname(backup_file)):
            os.makedirs(os.path.dirname(backup_file))
        print('Downloading {}'.format(backup_file))
        bucket.download_file(obj.key, backup_file)

    os.chdir(os.path.join(os.path.expanduser('~'), 'frappe-bench', 'sites'))

def main():
    backup_dir = get_backup_dir()

    if len(list_directories(backup_dir)) == 0:
        pull_backup_from_s3()

    for site in list_directories(backup_dir):
        site_slug = site.replace('.','_')
        backups = [datetime.datetime.strptime(backup, DATE_FORMAT) for backup in list_directories(os.path.join(backup_dir,site))]
        latest_backup = max(backups).strftime(DATE_FORMAT)
        files_base = os.path.join(backup_dir, site, latest_backup, '')
        files_base += latest_backup + '-' + site_slug
        if site in get_sites():
            restore_database(files_base, site)
            restore_private_files(files_base)
            restore_files(files_base)
        else:
            mariadb_root_password = os.environ.get('MYSQL_ROOT_PASSWORD')
            if not mariadb_root_password:
                print('Variable MYSQL_ROOT_PASSWORD not set')
                exit(1)
            mariadb_root_username = os.environ.get('DB_ROOT_USER', 'root')
            database_file = files_base + '-database.sql.gz'

            site_config = get_conf_params(
                db_name='_' + hashlib.sha1(site.encode()).hexdigest()[:16],
                db_password=random_string(16)
            )

            frappe.local.site = site
            frappe.local.sites_path = os.getcwd()
            frappe.local.site_path = os.getcwd() + '/' + site
            make_conf(
                db_name=site_config.get('db_name'),
                db_password=site_config.get('db_password'),
            )
            make_site_dirs()
            restore_database(files_base, site)
            restore_private_files(files_base)
            restore_files(files_base)

    exit(0)

if __name__ == "__main__":
    main()
