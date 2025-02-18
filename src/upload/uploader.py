"""
This file is meant to be used on Amazon S3. But for the purpose of
project reinvent, we are using Google Cloud Storage. Find more details here:
https://cloud.google.com/storage/docs/migrating
"""
import contextlib
import os.path
import io

from PIL import Image
from uuid import uuid4
from werkzeug.utils import secure_filename

from sqlalchemy import create_engine
from sqlalchemy import text
import boto3
from botocore.client import Config


def s3_path2url(path):
    """
    Convert path to a url
    """
    from main import app
    return os.path.join('https://', app.config['S3_HOST'], app.config['S3_BUCKET'], path)


def s3_connection():
    """
    Connection object
    """
    from main import app
    session = boto3.session.Session()
    return session.client('s3',
                      region_name=app.config['S3_REGION'],
                      endpoint_url=f"https://{app.config['S3_HOST']}",
                      aws_access_key_id=app.config['S3_KEY'],
                      aws_secret_access_key=app.config['S3_SECRET'])

def s3_bucket_name():
    from main import app
    return app.config['S3_BUCKET']

def s3_create_key_name(dir="", ext=""):
    """
    Create a key name
    """
    return os.path.join(dir, uuid4().hex + ext)

def s3_upload2(src, destkeyname, acl="public-read"):
    """
    Simple API to upload content to a key
    """
    # FIXME: multipart
    if not hasattr(src, 'read'):
        data = src
    else:
        data = src.read()
    s3 = s3_connection()
    s3.put_object(Bucket=s3_bucket_name(), Key=destkeyname, Body=data, ACL=acl)

    # url it was uploaded to
    return s3_path2url(destkeyname)




'''
Although we use S3 APIs here, we internally use the Google Cloud host.
Reference: https://cloud.google.com/storage/docs/migrating
'''
def _get_bucket():
    from main import app
    s3 = s3_connection()
    # boto3 does not have an equivalent to get_bucket without using resources which are higher level
    # we can construct the bucket name directly
    return s3_bucket_name()



def s3_upload(source_file,
        file_blob=None,
        destination_filename='',
        upload_dir=None,
        sub_dir='',
        acl='public-read'):
    """ Uploads File Object to Amazon S3
        Expects following app.config attributes to be set:
            S3_KEY              :   S3 API Key
            S3_SECRET           :   S3 Secret Key
            S3_BUCKET           :   What bucket to upload to
            S3_UPLOAD_DIRECTORY :   Which S3 Directory.

        sub_dir: should always begin with '/' and should not end with '/'.

        The default sets the access rights on the uploaded file to
        public-read.  It also generates a unique filename via
        the uuid4 function combined with the file extension from
        the source file if destination_filename not provided.
    """
    from main import app
    upload_dir = upload_dir or app.config["S3_UPLOAD_DIRECTORY"]
    source_filename = secure_filename(source_file.filename)
    source_extension = os.path.splitext(source_filename)[1]

    if not destination_filename:
        destination_filename = uuid4().hex + source_extension

    # Connect to S3 and upload file.
    bucket_name = _get_bucket()
    s3 = s3_connection()

    upload_path = os.path.join(upload_dir, sub_dir, destination_filename)
    if not file_blob:
        file_blob = source_file.read()

    s3.put_object(Bucket=bucket_name, Key=upload_path, Body=file_blob, ACL=acl)

    return s3_path2url(upload_path)


"""
def s3_upload_from_url(source_url,
        source_extension='.jpg',
        destination_filename='',
        upload_dir=None,
        sub_dir='',
        acl='public-read'):
     Uploads A file from url to Amazon S3
        sub_dir: should always begin with '/' and should not end with '/'.

    upload_dir = upload_dir or app.config["S3_UPLOAD_DIRECTORY"]
    if not destination_filename:
        destination_filename = uuid4().hex + source_extension

    if not app.config['S3_UPLOAD_ENABLE']:
        full_path = os.path.join(app.static_folder, 'uploads' + sub_dir + '/')
        util.download_file(source_url, full_path + destination_filename)
        return destination_filename

    response = requests.get(source_url)
    if response.status_code == 200:
        bucket = _get_bucket()
        upload_dir = upload_dir + sub_dir
        key = bucket.new_key("/".join([upload_dir, destination_filename]))
        key.set_contents_from_string(response.content)
        key.set_acl(acl)
    return destination_filename
"""


def s3_resize_and_upload(source_file,
        destination_filename='',
#        x1=0, y1=0, w=0, h=0, rotation=0,
        w_size=2160, h_size=1080,
        upload_dir=None,
        sub_dir='',
        acl='public-read'):
    """ Uploads File Object to Amazon S3
        Expects following app.config attributes to be set:
            S3_KEY              :   S3 API Key
            S3_SECRET           :   S3 Secret Key
            S3_BUCKET           :   What bucket to upload to
            S3_UPLOAD_DIRECTORY :   Which S3 Directory.
        The default sets the access rights on the uploaded file to
        public-read.  It also generates a unique filename via
        the uuid4 function combined with the file extension from
        the source file if destination_filename not provided.
    """
    from main import app
    upload_dir = upload_dir or app.config["S3_UPLOAD_DIRECTORY"]
    file_blob = source_file.read()
    original_path = s3_upload(source_file,
            file_blob,
            destination_filename,
            upload_dir, sub_dir, acl)

    original_filename = os.path.split(original_path)[1]
    source_filename = secure_filename(source_file.filename)
    source_extension = os.path.splitext(source_filename)[1]

    destination_filename = (
            os.path.splitext(original_filename)[0]
            + '_resized'
            + source_extension)

    upload_buffer = io.BytesIO(file_blob)
    image = Image.open(upload_buffer)
    format = image.format
    if image.format not in ['JPEG', 'PNG']:
        raise UserWarning("Invalid image")
    '''
    if w <=0 or h <=0 or x1 <0 or y1 <0:
        raise UserWarning("Invalid dimension")

    if image.size[0] < x1+w or image.size[1] < y1+h:
        # Image is smaller than the crop constraints. Let's use image size
        w = image.size[0] - x1
        h = image.size[1] - y1
        if w <= 0 or h <= 0:
            raise UserWarning("Invalid crop constraints")

    box = (x1, y1, (x1+w), (y1+h))
    image = image.crop(box)
    '''
    image.thumbnail((w_size, h_size), Image.LANCZOS)
    #if rotation:
    #    image = image.rotate(rotation)

    # Connect to S3 and upload file.
    bucket_name = _get_bucket()
    output_buffer = io.BytesIO()
    image.save(output_buffer, format)
    upload_dir = upload_dir + sub_dir
    upload_path = "/".join([upload_dir, destination_filename])

    s3 = s3_connection()
    s3.put_object(Bucket=bucket_name, Key=upload_path, Body=output_buffer.getvalue(), ACL=acl)

    return s3_path2url(upload_path)


'''
Also uploads to S3. Resizes based on the size provided.
'''
def s3_resize(path, resize_path, size, user_id_str=''):
    from . import image_sizes
    size_map = image_sizes.MAP.get(size)

    # Download from s3.
    bucket_name = _get_bucket()
    s3 = s3_connection()
    try:
        response = s3.get_object(Bucket=bucket_name, Key=path[1:])
        input_buffer = io.BytesIO(response['Body'].read())
    except Exception as e:
        raise UserWarning("Invalid image: " + path + " user_id:" + user_id_str)
    image = Image.open(input_buffer)
    format = image.format
    image.thumbnail((size_map.get('width'), size_map.get('height')),
            Image.LANCZOS)

    output_buffer = io.BytesIO()
    image.save(output_buffer, format)

    s3.put_object(Bucket=bucket_name, Key=resize_path[1:], Body=output_buffer.getvalue(), ACL='public-read')
