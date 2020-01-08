#!/usr/bin/env python3.7

# standard
import os
import io
import math
import sys
import stat
from stat import S_ISDIR
import subprocess
from pathlib import Path
import os.path
import pprint
from string import Template
import shutil
import re
import json
import logging
import time
import difflib
from datetime import datetime
from datetime import timedelta
from datetime import date
import threading
import base64
import atexit
import ast

# third party
import boto3
import botocore
from botocore.exceptions import ClientError
from boto3.s3.transfer import TransferConfig
import requests
import paramiko
from jsondiff import diff
from deepdiff import DeepDiff
import slack
import pytz
from pytz import timezone

# To run locally:
##################

# AWS IAM permissions:
# make sure you have the right keys on your local ~/.aws/credentials

# Replace:
# def main(event=None, context=None):

# with:
# def main():

# to run this code from your local terminal use:
# ```bash
# AWS_PROFILE=my-aws-profile ./wp-migration
# ```

# two ways to switch session:

# (1)
# session = boto3.Session(profile_name='jenkins-on-msge-od-nonprod', region_name="us-east-1")

# then:

# ec2 = session.resource("ec2")
# or:
# s3 = session.client('s3')

# (2) Single line:
# boto3.setup_default_session(profile_name='jenkins-on-msge-od-nonprod', region_name="us-east-1")

# then:

# sqs = boto3.client("sqs")
# or:
# ec2 = boto3.resource('ec2')

###########################################################################################
###########################################################################################
###########################################################################################
###########################################################################################
##### FUNCTIONS
###########################################################################################
###########################################################################################
###########################################################################################
###########################################################################################

def send_slack_notification_with_text(slack_api_key, slack_message, slack_channel):

  client = slack.WebClient(token=slack_api_key)

  # to send text:
  client.chat_postMessage(channel=slack_channel, text=slack_message)

def formatTimeDelta(td):
    deltaDays = td.days
    minutes, seconds = divmod(td.seconds + td.days * 86400, 60)
    hours, minutes = divmod(minutes, 60)
    millisecs = (td.microseconds/1000)
    return '{:02d} Days {:02d}:{:02d}:{:02d}.{:03d}'.format(deltaDays, hours, minutes, seconds, int(millisecs))

def humanReadableDataSize(nbytes):
    suffixes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    i = 0
    while nbytes >= 1024 and i < len(suffixes)-1:
        nbytes /= 1024.
        i += 1
    f = ('%.2f' % nbytes).rstrip('0').rstrip('.')
    return '%s %s' % (f, suffixes[i])

def openSFTPConToWpe(ftp_host, ftp_port, ftp_username, ftp_password):
    
    try:
        logger.info(
            "SFTP'ing into wpe install:\nHOST: {}:{}\nUsername: {}".format(
                ftp_host, 
                ftp_port, 
                ftp_username
            )
        )
        transport = paramiko.Transport((ftp_host, ftp_port))
        logger.info("Connection successfull!")
    except Exception as error:
        logger.error("SFTP Connection Error.\n{}".format(error))
        return "conn_error"
    
    try:
        transport.connect(None, ftp_username, ftp_password)
        logger.info("Authentication successfull!")
    except Exception as error:
        logger.error("SFTP Authentication Error.".format(ftp_host, ftp_port, error))
        return "auth_error"

    sftpConnection = paramiko.SFTPClient.from_transport(transport)
    transport.set_keepalive(60)
    return sftpConnection

def openSFTPConToEc2(ec2inctanceID, awsProfile, ec2Region, ftp_host, ftp_port, ftp_username, ftp_password):

    ec2SFTPDetails = getEC2DetailsById(ec2inctanceID, awsProfile, ec2Region)

    ec2Id = ec2SFTPDetails["InstanceId"]
    ec2Region = ec2SFTPDetails["InstanceRegion"]
    ec2KeyName = ec2SFTPDetails["KeyName"]
    ec2PublicIp = ec2SFTPDetails["InstancePublicIp"]
    ec2PrivateIp = ec2SFTPDetails["instancePrivateIp"]
    ec2AccountAlias = ec2SFTPDetails["Ec2AccountAlias"]
    ec2AccountId = ec2SFTPDetails["Ec2AccountId"]
    ec2Name = ec2SFTPDetails['InstanceNameTag']

    try:
        logger.info(
            "SFTP'ing into EC2:\nAWS Account: {}\nRegion: {}\nID: {}\nName: {}\nPublic IP: {}\nSFTP username: {}".format(
                getAWSAccountAliasAndID(ec2AccountAlias),
                ec2Region,
                ec2Id,
                ec2Name,
                ec2PublicIp,
                ftp_username
            )
        )
        transport = paramiko.Transport((ftp_host, ftp_port))
        logger.info("Connection successfull!")
    except Exception as error:
        logger.error("SFTP Connection Error.\n{}".format(error))
        return "conn_error"
    
    try:
        transport.connect(None, ftp_username, ftp_password)
        logger.info("Authentication successfull!")
    except Exception as error:
        logger.error("SFTP Authentication Error.")
        return "auth_error"

    sftpConnection = paramiko.SFTPClient.from_transport(transport)
    transport.set_keepalive(60)
    return sftpConnection

def transfer_chunk_from_ftp_to_s3(
        ftp_file, 
        s3_connection, 
        multipart_upload, 
        bucket_name, 
        ftp_file_path, 
        s3_file_path, 
        part_number, 
        chunk_size
    ):
    logger.info("transfer_chunk_from_ftp_to_s3")
    start_time = time.time()
    chunk = ftp_file.read(int(chunk_size))
    part = s3_connection.upload_part(
        Bucket = bucket_name,
        Key = s3_file_path,
        PartNumber = part_number,
        UploadId = multipart_upload['UploadId'],
        Body = chunk,
    )
    end_time = time.time()
    total_seconds = end_time - start_time
    logger.info('speed is {} kb/s total seconds taken {}'.format(math.ceil((int(chunk_size) /1024) / total_seconds), total_seconds))
    part_output = {
        'PartNumber': part_number,
        'ETag': part['ETag']
    }
    return part_output

def mkdir_p(sftp, remote_directory):
    """Change to this directory, recursively making new folders if needed.
    Returns True if any folders were created."""
    if remote_directory == '/':
        # absolute path so change directory to root
        sftp.chdir('/')
        return
    if remote_directory == '':
        # top-level relative directory must exist
        return
    try:
        sftp.chdir(remote_directory) # sub-directory exists
    except IOError:
        dirname, basename = os.path.split(remote_directory.rstrip('/'))
        mkdir_p(sftp, dirname) # make parent directories
        sftp.mkdir(basename) # sub-directory missing, so created it
        sftp.chdir(basename)
        return True

def transfer_file_from_sftp_to_sftp(sftpConnection, sftpConnectionDest, sftp_file_path_source, sftp_file_path_dest, chunk_size):

    sftp_file_path_dest_dir_name = os.path.dirname(sftp_file_path_dest)
    sftp_file_path_dest_file_name = os.path.basename(sftp_file_path_dest)

    ftp_file = sftpConnection.file(sftp_file_path_source, 'r')
    ftp_file_size = ftp_file._get_size()

    # makes ure all directories and child directories are created before copying:
    mkdir_p(sftpConnectionDest, sftp_file_path_dest_dir_name)

    if ftp_file_size <= int(chunk_size):
        #upload file in one go
        logger.info('Transferring File: {}'.format(sftp_file_path_source))
        logger.info('To File: {}'.format(sftp_file_path_dest))
        ftp_file_data = io.BytesIO(ftp_file.read())
        sftpConnectionDest.putfo(ftp_file_data, sftp_file_path_dest_file_name, file_size=0, callback=None, confirm=False)
        logger.info('Success!')
        ftp_file.close()

    else:
        logger.info('Transferring Chunks...')
        #upload file in chunks
        chunk_count = int(math.ceil(ftp_file_size / float(chunk_size)))

        with sftpConnectionDest.open(sftp_file_path_dest_file_name, "w") as f:

            for i in range(chunk_count):
                start_time = time.time()
                chunk = ftp_file.read(int(chunk_size))
                logger.info('Transferring chunk {} of {}'.format((i + 1),chunk_count))
                f.write(chunk)
                end_time = time.time()
                total_seconds = end_time - start_time
                logger.info('speed is {} kb/s total seconds taken {}'.format(math.ceil((int(chunk_size) /1024) / total_seconds), total_seconds))

            # logger.info('Chunk {} Transferred Successfully!'.format(i + 1))

        # logger.info('All chunks Transferred to S3 bucket! File Transfer successful!')
        logger.info('Success!')
        ftp_file.close()

def transfer_file_from_ftp_to_s3(sftpConnection, bucket_name, ftp_file_path, s3_file_path, chunk_size):
    # Source:
    # https://github.com/kirankumbhar/File-Transfer-FTP-to-S3-Python/blob/master/ftp_to_s3.py
    # https://medium.com/better-programming/transfer-file-from-ftp-server-to-a-s3-bucket-using-python-7f9e51f44e35
    # logger.info("transfer_file: {} _from_ftp_to_s3: {} as {}".format(ftp_file_path, bucket_name, s3_file_path))

    # sftpConnection = openSFTPConnection(WPENGINE_SFTP_HOST, int(WPENGINE_SFTP_HOST_PORT), ftp_username, ftp_password)
    
    ftp_file = sftpConnection.file(ftp_file_path, 'r')

    boto3.setup_default_session(profile_name=getJenkinsProfile(MSG_WP_BACKUP_S3_BUCKET_ACCOUNT_ID_OR_ALIAS), region_name=MSG_WP_BACKUP_S3_BUCKET_REGION)
    s3_connection = boto3.client('s3', region_name=MSG_WP_BACKUP_S3_BUCKET_REGION)
    ftp_file_size = ftp_file._get_size()
    try:
        s3_file = s3_connection.head_object(Bucket = bucket_name, Key = s3_file_path)
        if s3_file['ContentLength'] == ftp_file_size:
            logger.info('File Already Exists in S3 bucket')
            ftp_file.close()
            return
    except Exception as e:
        pass
    if ftp_file_size <= int(chunk_size):
        #upload file in one go
        logger.info('Transferring File...')
        ftp_file_data = io.BytesIO(ftp_file.read())
        s3_connection.upload_fileobj(ftp_file_data, bucket_name, s3_file_path)
        logger.info('Success!')
        ftp_file.close()

    else:
        logger.info('Transferring Chunks...')
        #upload file in chunks
        chunk_count = int(math.ceil(ftp_file_size / float(chunk_size)))
        multipart_upload = s3_connection.create_multipart_upload(Bucket = bucket_name, Key = s3_file_path)
        parts = []
        for  i in range(chunk_count):
            logger.info('Transferring chunk {} of {}'.format((i + 1),chunk_count))
            part = transfer_chunk_from_ftp_to_s3(
                ftp_file, 
                s3_connection, 
                multipart_upload, 
                bucket_name, 
                ftp_file_path, 
                s3_file_path, 
                i + 1, 
                chunk_size
            )
            parts.append(part)
            # logger.info('Chunk {} Transferred Successfully!'.format(i + 1))

        part_info = {
            'Parts': parts
        }
        s3_connection.complete_multipart_upload(
        Bucket = bucket_name,
        Key = s3_file_path,
        UploadId = multipart_upload['UploadId'],
        MultipartUpload = part_info
        )
        # logger.info('All chunks Transferred to S3 bucket! File Transfer successful!')
        logger.info('Success!')
        ftp_file.close()

def getSSHCommandResults (commandDescription, ec2Id, ec2Name, ec2Region, ec2AwsAccountAlias, ec2AwsAccountId, commandToBeProcessed, command_stdout, command_stderror, command_exit_status):
    
    pretty_stdout_output = command_stdout.decode(encoding='UTF-8') # ugly_stdout_output = stdout_output.recv(65535)
    pretty_stderr_output = command_stderror.decode(encoding='UTF-8')

    resultDict = dict()
    # filter results:
    if command_exit_status == 0:
        logger.info("===================================================================================")
        logger.info("COMMAND DESCRIPTION: {}".format(commandDescription))
        logger.info("EXECUTING REMOTE COMMAND: {}".format(commandToBeProcessed))
        logger.info("ON EC2: {} - {} - {}".format(ec2Id, ec2Name, ec2Region))
        logger.info("AWS ACCOUNT: {}".format(getAWSAccountAliasAndID(ec2AwsAccountAlias)))
        logger.info("COMMAND EXIT STATUS: {} (SUCCESS!)".format(command_exit_status))
        logger.info("COMMAND STDOUT:")
        logger.info(pretty_stdout_output)
        logger.info("===================================================================================")
        resultDict['exit_code_status'] = "SUCCESS"
        resultDict['exit_code'] = command_exit_status
        resultDict['std_out'] = pretty_stdout_output
        resultDict['std_error'] = "-"
        return resultDict
    else:
        logger.info("===================================================================================")
        logger.info("COMMAND DESCRIPTION: {}".format(commandDescription))
        logger.info("EXECUTING REMOTE COMMAND: {}".format(commandToBeProcessed))
        logger.info("ON EC2: {} - {} - {}".format(ec2Id, ec2Name, ec2Region))
        logger.info("AWS ACCOUNT: {}".format(getAWSAccountAliasAndID(ec2AwsAccountId)))
        logger.info("COMMAND EXIT STATUS: {} (FAIL!)".format(command_exit_status))
        logger.info("COMMAND STDERROR:")
        logger.info(pretty_stderr_output)
        logger.info("===================================================================================")
        resultDict['exit_code_status'] = "FAIL"
        resultDict['exit_code'] = command_exit_status
        resultDict['std_out'] = "-"
        resultDict['std_error'] = pretty_stderr_output
        return resultDict

def buildWPEWpVersionScriptFile(
    getWPEWPVersionScripFileName,
    WPENGINE_SFTP_HOST,
    WPENGINE_SFTP_HOST_PORT,
    WPENGINE_SFTP_USERNAME,
    WPENGINE_SFTP_PASSWORD,
    WPENGINE_CERT_URL, 
    WPENGINE_CERT_LOCAL_PATH,
    jenkinsInstanceID, 
    jenkinsAWSProfile, 
    jenkinsECsRegion, 
    jenkinsSSHClient
):

    # get wpe db credentials from wp-config.php

    # CONNECT TO WPENGINE SFTP HOST:
    sftpConnection = openSFTPConToWpe(
        WPENGINE_SFTP_HOST, 
        int(WPENGINE_SFTP_HOST_PORT), 
        WPENGINE_SFTP_USERNAME, 
        WPENGINE_SFTP_PASSWORD
    )
   
    if sftpConnection == 'conn_error':
        logger.error('Failed to connect FTP Server: {} ... EXITING!'.format(WPENGINE_SFTP_HOST))
        sys.exit()

    if sftpConnection == 'auth_error':
        logger.error('Incorrect username or password for: {} ... EXITING!'.format(WPENGINE_SFTP_HOST))
        sys.exit()

    sftpConnection.get("/wp-config.php", "wp-config.php")

    # WPE_DB_USERNAME=`cat wp-config.php | grep DB_USER | cut -d \' -f 4`
    command = """cat wp-config.php | grep DB_USER | cut -d \\' -f 4"""
    commandResult = runLocalCommand("Extract WPE DB USER from wp-config.php", command)
    WPE_DB_USERNAME = ""
    if commandResult["commandReturnCode"] == 0:
        WPE_DB_USERNAME = commandResult["commandStdout"].strip()
    else:
        logger.error("Unable to retrieve WPE DB USER from wp-config.php")

    # WPE_DB_PASSWORD=`cat wp-config.php | grep DB_PASSWORD | cut -d \' -f 4`
    command = """cat wp-config.php | grep DB_PASSWORD | cut -d \\' -f 4"""
    commandResult = runLocalCommand("Extract WPE DB PASSWORD from wp-config.php", command)
    WPE_DB_PASSWORD = ""
    if commandResult["commandReturnCode"] == 0:
        WPE_DB_PASSWORD = commandResult["commandStdout"].strip()
    else:
        logger.error("Unable to retrieve WPE DB PASSWORD from wp-config.php")

    jenkinsCommand = "curl {} -o {}".format(
        WPENGINE_CERT_URL,
        WPENGINE_CERT_LOCAL_PATH
    )
    commandResult = runCommandOnEc2(
        "download wpe cert to jenkins", 
        jenkinsCommand,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    scriptFolder = Path('/tmp')
    scriptPath = scriptFolder.joinpath(getWPEWPVersionScripFileName)

    my_script_string = """#!/bin/bash
mysql -h {} \\
 -D {} -u {} -p{} -P13306 --ssl-ca={} -e\\
  "SELECT option_value FROM wp_options WHERE option_name='_site_transient_update_core';"\\
   --skip-column-names | awk -F '\"' '{{ print $42 }}'
    """.format(
        WPENGINE_SFTP_HOST,
        WPE_DB_NAME,
        WPE_DB_USERNAME,
        WPE_DB_PASSWORD,
        WPENGINE_CERT_LOCAL_PATH
    )

    with open (scriptPath, 'w') as myFile:
        myFile.write(my_script_string)    

def buildAWSWpVersionScriptFile(
    getWASWPVersionScripFileName,
    MSG_WP_RDS_HOST_END_POINT,
    TFVARS_WP_DB_NAME,
    TFVARS_WP_DB_USERNAME,
    TFVARS_WP_DB_PASSWORD
):

    scriptFolder = Path('/tmp')
    scriptPath = scriptFolder.joinpath("get_aws_wp_version.sh")

    my_script_string = """#!/bin/bash
mysql -h {} \\
 -D {} -u {} -p{} -e\\
  "SELECT option_value FROM wp_options WHERE option_name='_site_transient_update_core';"\\
   --skip-column-names | awk -F '\"' '{{ print $42 }}'
    """.format(
        MSG_WP_RDS_HOST_END_POINT,
        TFVARS_WP_DB_NAME,
        TFVARS_WP_DB_USERNAME,
        TFVARS_WP_DB_PASSWORD
    )

    with open (scriptPath, 'w') as myFile:
        myFile.write(my_script_string)

def buildUpdateWpWebsiteURLFiles():

    # create the sql bash script file:
    ########################################
    scriptFolder = Path('/tmp')
    mySqlScriptPath = scriptFolder.joinpath("update_wp_site_url.sql")

    my_sql_script_file = """
UPDATE TFVARS_WP_TABLE_PREFIX_options SET option_value = replace(option_value, @oldurl, @newurl) WHERE option_name = 'home' OR option_name = 'siteurl';

UPDATE TFVARS_WP_TABLE_PREFIX_posts SET guid = replace(guid, @oldurl, @newurl);

UPDATE TFVARS_WP_TABLE_PREFIX_posts SET post_content = replace(post_content, @oldurl, @newurl);

UPDATE TFVARS_WP_TABLE_PREFIX_postmeta SET meta_value = replace(meta_value, @oldurl, @newurl);"""

    # replace TFVARS_WP_TABLE_PREFIX 
    my_sql_script_file = my_sql_script_file.replace("TFVARS_WP_TABLE_PREFIX_", TFVARS_WP_TABLE_PREFIX)

    # save file
    with open (mySqlScriptPath, 'w') as myFile:
        myFile.write(my_sql_script_file)

    # create the bash script file:
    ########################################
    scriptFolder = Path('/tmp')
    myBashScriptPath = scriptFolder.joinpath("update_wp_site_url.sh")


    my_bash_script_file = """
#!/bin/bash

mysql -v -v -u {} -h {} -p{} {} -A -e "set @oldurl='${{from_url}}'; set @newurl='${{to_url}}'; source /tmp/update_wp_site_url.sql;"
""".format(
    TFVARS_WP_DB_USERNAME,
    MSG_WP_RDS_HOST_END_POINT,
    MSG_WP_RDS_DB_PASSWORD,
    TFVARS_WP_DB_NAME,

)
    # save file
    with open (myBashScriptPath, 'w') as myFile:
        myFile.write(my_bash_script_file)

def openSSHConToEC2(ec2inctanceID, awsProfile, ec2Region):

    ec2SSHDetails = getEC2DetailsById(ec2inctanceID, awsProfile, ec2Region)

    ec2Id = ec2SSHDetails["InstanceId"]
    ec2Region = ec2SSHDetails["InstanceRegion"]
    ec2KeyName = ec2SSHDetails["KeyName"]
    ec2PublicIp = ec2SSHDetails["InstancePublicIp"]
    ec2PrivateIp = ec2SSHDetails["instancePrivateIp"]
    ec2AccountAlias = ec2SSHDetails["Ec2AccountAlias"]
    ec2AccountId = ec2SSHDetails["Ec2AccountId"]
    ec2Name = ec2SSHDetails['InstanceNameTag']
    ec2User = ec2SSHDetails["InstanceUser"]
    ec2LocalKeyFilePath = str(MSG_KEYS_LOCAL_FOLDER) + "/" + ec2KeyName
    # download key from s3:
    download_s3_file(EC2_KEYS_BUCKET_NAME, ec2KeyName, ec2LocalKeyFilePath)
    # set file permissions:
    os.chmod(ec2LocalKeyFilePath, stat.S_IRUSR | stat.S_IWUSR)

    try:

        sshClient = paramiko.SSHClient()
        sshKey = paramiko.RSAKey.from_private_key_file(ec2LocalKeyFilePath)
        sshClient.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        logger.info(
            "SSH'ing into EC2:\nAWS Account: {}\nRegion: {}\nID: {}\nName: {}\nPublic IP: {}\nKey Pair Name: {}".format(
                getAWSAccountAliasAndID(ec2AccountAlias),
                ec2Region,
                ec2Id,
                ec2Name,
                ec2PublicIp,
                ec2KeyName
            )
        )
        sshClient.connect( hostname = ec2PublicIp, username = ec2User, pkey = sshKey )

    except paramiko.ssh_exception.AuthenticationException as e:

        errorMessage = "Unable to SSH into: {}@{}\n".format(ec2User, ec2PublicIp)
        errorMessage = errorMessage + "Instance: {}\n".format(ec2inctanceID, )
        errorMessage = errorMessage + "AWS Profile Used: {}\n".format(awsProfile)
        errorMessage = errorMessage + "Error:\n{}".format(e)
        logger.error(errorMessage)
        sys.exit()

    return sshClient

def runCommandOnEc2(commandDescription, ec2Command, ec2Id, ec2AWSProfile, ec2Region, sshClient):
    ec2Details = getEC2DetailsById(ec2Id, ec2AWSProfile, ec2Region)
    ec2Id = ec2Details["InstanceId"]
    ec2Name = ec2Details["InstanceNameTag"]
    ec2AwsAccountAlias = ec2Details["Ec2AccountAlias"]
    ec2AwsAccountId = ec2Details["Ec2AccountId"]

    stdin , stdout, stderr = sshClient.exec_command(ec2Command)
    commandResultsDict = getSSHCommandResults(commandDescription, ec2Id, ec2Name, ec2Region, ec2AwsAccountAlias, ec2AwsAccountId, ec2Command, 
    stdout.read(), stderr.read(), stdout.channel.recv_exit_status())
    return commandResultsDict

def getEC2DetailsById(instanceId, awsProfileName, awsRegion):

    boto3.setup_default_session(profile_name=awsProfileName, region_name=awsRegion)
    awsAccountAlias = boto3.client('iam').list_account_aliases()['AccountAliases'][0]
    awsAccountId = boto3.client('sts').get_caller_identity().get('Account')

    ec2 = boto3.resource('ec2')
    ec2instance = ec2.Instance(instanceId)
    ec2instance.load()

    instanceSSHDetailsDict = dict()
    instanceSSHDetailsDict["InstanceId"] = instanceId
    instanceSSHDetailsDict["InstanceRegion"] = awsRegion
    instanceSSHDetailsDict["KeyName"] = ec2instance.key_name
    instanceSSHDetailsDict["InstancePublicIp"] = ec2instance.public_ip_address
    instanceSSHDetailsDict["instancePrivateIp"] = ec2instance.private_ip_address
    instanceSSHDetailsDict["imageId"] = ec2instance.image_id
    instanceSSHDetailsDict["Ec2AccountAlias"] = awsAccountAlias
    instanceSSHDetailsDict["Ec2AccountId"] = awsAccountId
    instanceSSHDetailsDict["AwsProfileUsed"] = awsProfileName

    for tags in ec2instance.tags:
        if tags["Key"] == 'Name':
            instanceSSHDetailsDict['InstanceNameTag'] = tags["Value"]
    
    instanceSSHDetailsDict["InstanceUser"] = "ec2-user" 

    return instanceSSHDetailsDict

def download_s3_bucket(client, resource, local='/tmp', bucket='your_bucket'):
    # this will only work if all objects are in the bucket root,
    # if there are any subdirectories it will fail... must be reviewed.
    paginator = client.get_paginator('list_objects')
    for result in paginator.paginate(Bucket=bucket, Delimiter='/'):
        if result.get('CommonPrefixes') is not None:
            for subdir in result.get('CommonPrefixes'):
                download_s3_bucket(client, resource, local, bucket)
        for file in result.get('Contents', []):
            dest_pathname = os.path.join(local, file.get('Key'))
            if not os.path.exists(os.path.dirname(dest_pathname)):
                os.makedirs(os.path.dirname(dest_pathname))
            resource.meta.client.download_file(bucket, file.get('Key'), dest_pathname)

def download_s3_file(bucket, key, local_file_path):

    try:
        s3_client = boto3.client('s3')
        s3_client.download_file(bucket, key, local_file_path)
        logger.info('downloaded the file: {} from S3: https://s3.console.aws.amazon.com/s3/buckets/{}'.format(key,bucket))
        
    except ClientError as e:
        logger.error('Unable to download the file: {} from S3: https://s3.console.aws.amazon.com/s3/buckets/{}'.format(key,bucket))
        logger.error(e)

def upload_file_to_s3(bucket, key, local_file_path):

    try:

        s3_client = boto3.client('s3')
        s3_client.upload_file(local_file_path, bucket, key)
        logger.info('uploaded the file: {} to S3: https://s3.console.aws.amazon.com/s3/buckets/{}'.format(key,bucket))
        
    except ClientError as e:
        logger.error('Unable to upload the file: {} from S3: https://s3.console.aws.amazon.com/s3/buckets/{}'.format(key,bucket))
        logger.error(e)

def create_s3_folder(bucket, key):

    try:

        s3_client = boto3.client('s3')
        s3_client.put_object(Bucket=bucket, Key=key)
        logger.info('Creating the S3 directory: {} on S3: https://s3.console.aws.amazon.com/s3/buckets/{}'.format(key,bucket))
        
    except ClientError as e:
        logger.error('Unable to create the directory: {} on S3: https://s3.console.aws.amazon.com/s3/buckets/{}'.format(key,bucket))
        logger.error(e)

def sftp_walk(sftp, remotepath):
    path=remotepath
    files=[]
    folders=[]
    for f in sftp.listdir_attr(remotepath):
        if S_ISDIR(f.st_mode):
            folders.append(f.filename)
        else:
            files.append(f.filename)
    if files:
        yield path, files
    for folder in folders:
        new_path=os.path.join(remotepath,folder)
        for x in sftp_walk(sftp, new_path):
            yield x

def update_migration_status(wpeInstallName, keyToUpdate, keyValueToUpdate):
    # example:
    # update_migration_status(wpeInstallName, websiteTerraformStatus, "COMPLETE")

    # avaialble keys
    # "wpeInstallName": "msgaspireqa",
    # "websiteTerraformStatus": "COMPLETE", 
    # "websiteContentCopyStatus": "COMPLETE",
    # "contentFilePermissionStatus": "COMPLETE", 
    # "contentFileOwnershipStatus": "COMPLETE", 
    # "dbRestoreStatus": "INCOMPLETE", 
    # "setWebsiteDomainInRoute53Status": "COMPLETE", 
    # "updateWebsiteURLinWPDBStatus": "INCOMPLETE"
    # "backupWPEWPDBToS3Status": "INCOMPLETE"

    logger.info("Saving migration status for the install {}... updating: {} to {}".format(wpeInstallName, keyToUpdate, keyValueToUpdate))

    def updateDictInDictList(wpeInstallName, dictList, keyValuePairToUpdate):
        for mydict in dictList:
            for k, v in mydict.items():
                if v == wpeInstallName:
                    mydict.update(keyValuePairToUpdate)

    # instantiate the dict.
    installProgressStatusDict = {}

    if os.stat("wpe-install-migration-progress.json").st_size != 0:
        with open("wpe-install-migration-progress.json") as f:
            # dictListData = ast.literal_eval(f.read())
            migrationStatusJson = json.load(f)
        
        installProgressStatusDict = next((item for item in migrationStatusJson["installs"] if item["wpeInstallName"] == wpeInstallName), None)

    else:
        installsRoot = {}
        installsRoot["installs"] = []
        installProgressStatusDict = {}

    if bool(installProgressStatusDict):

        updateDictInDictList(wpeInstallName, migrationStatusJson["installs"], {"{}".format(keyToUpdate): keyValueToUpdate})

        logger.info("{} updated to {}".format(keyToUpdate, keyValueToUpdate))

        with open("wpe-install-migration-progress.json", "w") as f:
            json.dump(migrationStatusJson, f)
            # f.write(str(migrationStatusJson["installs"]))

    else:
        # no progres entry found, this install migration is running for the first time:
        logger.info("The migration of {} is being processed for the first time, updating migration status...".format(wpeInstallName))
        installProgressStatusDict = {}
        installProgressStatusDict["wpeInstallName"] = wpeInstallName

        # initialize file with defaults:
        installProgressStatusDict["websiteTerraformStatus"] = "INCOMPLETE"
        installProgressStatusDict["websiteContentCopyStatus"] = "INCOMPLETE"
        installProgressStatusDict["contentFilePermissionStatus"] = "INCOMPLETE"
        installProgressStatusDict["contentFileOwnershipStatus"] = "INCOMPLETE"
        installProgressStatusDict["dbRestoreStatus"] = "INCOMPLETE"
        installProgressStatusDict["setWebsiteDomainInRoute53Status"] = "INCOMPLETE"
        installProgressStatusDict["updateWebsiteURLinWPDBStatus"] = "INCOMPLETE"
        installProgressStatusDict["backupWPEWPDBToS3Status"] = "INCOMPLETE"
        
        # update status where the user entered "complete", otherwise assign the status with the default status which is "incomplete"
        installProgressStatusDict["{}".format(keyToUpdate)] = keyValueToUpdate

        # save progress to file:
        migrationStatusJson["installs"].append(dict(installProgressStatusDict))
        # migrationStatusJson["installs"].append(installProgressStatusDict.copy())

        with open("wpe-install-migration-progress.json", "w") as f:
            json.dump(migrationStatusJson, f)
            # f.write(str(migrationStatusJson["installs"]))

 def get_migration_status(wpeInstallName):

    logger.info("retreiving migration status for the install: {}...".format(wpeInstallName))

    # "wpeInstallName": "msgaspireqa",
    # "websiteTerraformStatus": "COMPLETE", 
    # "websiteContentCopyStatus": "COMPLETE",
    # "contentFilePermissionStatus": "COMPLETE", 
    # "contentFileOwnershipStatus": "COMPLETE", 
    # "dbRestoreStatus": "INCOMPLETE", 
    # "setWebsiteDomainInRoute53Status": "COMPLETE", 
    # "updateWebsiteURLinWPDBStatus": "INCOMPLETE"
    # "backupWPEWPDBToS3Status": "INCOMPLETE"

    # instantiate the dict.
    installProgressStatusDict = {}

    if os.stat("wpe-install-migration-progress.json").st_size != 0:
        with open("wpe-install-migration-progress.json") as f:
            # dictListData = ast.literal_eval(f.read())
            migrationStatusJson = json.load(f)

        installProgressStatusDict = next((item for item in migrationStatusJson["installs"] if item["wpeInstallName"] == wpeInstallName), None)

    else:
        installProgressStatusDict = {}
 
    if bool(installProgressStatusDict):

        return installProgressStatusDict

    else:

        installProgressStatusDict = {}
        installProgressStatusDict["wpeInstallName"] = ""
        installProgressStatusDict["websiteTerraformStatus"] = ""
        installProgressStatusDict["websiteContentCopyStatus"] = ""
        installProgressStatusDict["contentFilePermissionStatus"] = ""
        installProgressStatusDict["contentFileOwnershipStatus"] = ""
        installProgressStatusDict["dbRestoreStatus"] = ""
        installProgressStatusDict["setWebsiteDomainInRoute53Status"] = ""
        installProgressStatusDict["updateWebsiteURLinWPDBStatus"] = ""
        installProgressStatusDict["backupWPEWPDBToS3Status"] = ""

        return installProgressStatusDict

def setFilePernissionsOnContentDir(
    WPENGINE_INSTALL_NAME,
    wpBastionKeyName,
    wpBastionEC2User,
    wpBastionPublicIp,
    WP_BASTION_WEBSITE_CONTENT_DIR,
    jenkinsInstanceID,
    jenkinsAWSProfile,
    jenkinsECsRegion,
    jenkinsSSHClient
):

    jenkinsCommand = "ssh -i ~/.ssh/{} {}@{} 'sudo chmod -Rv g+w {}'".format(
        wpBastionKeyName, 
        wpBastionEC2User, 
        wpBastionPublicIp, 
        WP_BASTION_WEBSITE_CONTENT_DIR
    )
    commandResult = runCommandOnEc2(
        "set dir permissions to writable before copying to content folder {}, this might take a very long time, please wait.".format(WP_BASTION_WEBSITE_CONTENT_DIR), 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )
    logger.info(commandResult["std_out"])

    #update migration status:
    update_migration_status(
        WPENGINE_INSTALL_NAME,
        "contentFilePermissionStatus",
        "COMPLETE"
    )

def setFileOwnershipOnContentDir(
    WPENGINE_INSTALL_NAME,
    wpBastionKeyName,
    wpBastionEC2User,
    wpBastionPublicIp,
    WP_BASTION_WEBSITE_CONTENT_DIR,
    jenkinsInstanceID,
    jenkinsAWSProfile,
    jenkinsECsRegion,
    jenkinsSSHClient
):

    # after creating the directory make sure the oenership and permissions allow for the upcoming copy operation:
    jenkinsCommand = "ssh -i ~/.ssh/{} {}@{} 'sudo chown -Rv wordpress:sftpusers {}'".format(
        wpBastionKeyName, 
        wpBastionEC2User, 
        wpBastionPublicIp, 
        WP_BASTION_WEBSITE_CONTENT_DIR
    )
    commandResult = runCommandOnEc2(
        "set dir ownership on the wp content folder {} to wordpress:sftpusers before copying content, this might take a very long time, please wait. {}".format(
            WP_BASTION_WEBSITE_CONTENT_DIR,

        ), 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )
    logger.info(commandResult["std_out"])

    #update migration status:
    update_migration_status(
        WPENGINE_INSTALL_NAME,
        "contentFileOwnershipStatus",
        "COMPLETE"
    )

def goodbye(completion_status, duration_message):

    migrationStatus = get_migration_status(WPENGINE_INSTALL_NAME)

    # migrationStatus["wpeInstallName"]
    # migrationStatus["websiteTerraformStatus"]
    # migrationStatus["websiteContentCopyStatus"]
    # migrationStatus["contentFilePermissionStatus"]
    # migrationStatus["contentFileOwnershipStatus"]
    # migrationStatus["dbRestoreStatus"]

    # we will rely on the local file only
    # # uplaod status file to s3
    # boto3.setup_default_session(profile_name='jenkins-on-msge-shared-prod', region_name="us-east-1")
    # upload_file_to_s3("msg-devops-scripts", "wpe-install-migration-progress.json", "wpe-install-migration-progress.json")

    slack_message = ""
    # uplaod log file
    boto3.setup_default_session(profile_name='jenkins-on-msge-shared-prod', region_name="us-east-1")
    s3_client = boto3.client('s3')
    s3ObjectKey = "logs/" + logFileName
    s3_client.upload_file(logFilePath, "msg-devops-scripts", s3ObjectKey)

    # provide a signed link for the log file
    response = s3_client.generate_presigned_url('get_object', Params={'Bucket': "msg-devops-scripts", 'Key': s3ObjectKey}, ExpiresIn=604800)
    logFileUrl = response

    CloudWatchlogsUrl = "https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#logStream:group=/aws/lambda/wp-migration;streamFilter=typeLogStreamPrefix"

    # send slack notifiction
    if WEBSITE_SUB_DOMAIN == "":
        migrated_wp_website_fqdn = "www." + WEBSITE_DOMAIN
    else:
        migrated_wp_website_fqdn = WEBSITE_SUB_DOMAIN + "." + WEBSITE_DOMAIN

        # "\n\n" + "link to CloudWatch Logs: " + CloudWatchlogsUrl + \

    if completion_status == "COMPLETE":
        slack_message = slack_message + "\n" + "Migration of the wpe install https://my.wpengine.com/installs/{} completed sucessfully!".format(WPENGINE_INSTALL_NAME) + \
        "\n\n" + duration_message + \
        "\n\n" + "link to logfile: " + logFileUrl + \
        "\n\n" + "link to new website: " + migrated_wp_website_fqdn + \
        "\n\n" + "link to new admin website: " + migrated_wp_website_fqdn + "/wp-login.php" \
        "\n\n" + "Terraform Env Status: " + migrationStatus["websiteTerraformStatus"] + \
        "\n\n" + "Content Migration Status: " + migrationStatus["websiteContentCopyStatus"] + \
        "\n\n" + "Content Directory File Permission Modification Status: " + migrationStatus["contentFilePermissionStatus"] + \
        "\n\n" + "Content Directory File Ownership Modification Status: " + migrationStatus["contentFileOwnershipStatus"] + \
        "\n\n" + "Content Directory File Permission Modification Status: " + migrationStatus["contentFileOwnershipStatus"] + \
        "\n\n" + "WP DB Restoration Status: " + migrationStatus["dbRestoreStatus"] + \
        "\n\n" + "Website domain Status: " + migrationStatus["setWebsiteDomainInRoute53Status"]

    if completion_status == "INCOMPLETE":
        slack_message = slack_message + "\n" + "Migration of the wpe install https://my.wpengine.com/installs/{} exited prematurely, check logs!".format(WPENGINE_INSTALL_NAME) + \
        "\n\n" + duration_message + \
        "\n\n" + "link to logfile: " + logFileUrl + \
        "\n\n" + "link to new website: " + migrated_wp_website_fqdn + \
        "\n\n" + "link to new admin website: " + migrated_wp_website_fqdn + "/wp-login.php"
        "\n\n" + "Terraform Env Status: " + migrationStatus["websiteTerraformStatus"] + \
        "\n\n" + "Content Migration Status: " + migrationStatus["websiteContentCopyStatus"] + \
        "\n\n" + "Content Directory File Permission Modification Status: " + migrationStatus["contentFilePermissionStatus"] + \
        "\n\n" + "Content Directory File Ownership Modification Status: " + migrationStatus["contentFileOwnershipStatus"] + \
        "\n\n" + "Content Directory File Permission Modification Status: " + migrationStatus["contentFileOwnershipStatus"] + \
        "\n\n" + "WP DB Restoration Status: " + migrationStatus["dbRestoreStatus"] + \
        "\n\n" + "Website domain Status: " + migrationStatus["setWebsiteDomainInRoute53Status"]

    slack_channel = "msg-wp-vul-updates"
    send_slack_notification_with_text(SLACK_API_KEY, slack_message, slack_channel)

    # remove credentials:
    logger.info("Removing credentials and keys")
    os.remove(AWS_SHARED_CREDENTIALS_FILE)
    # remove keys:
    shutil.rmtree(MSG_KEYS_LOCAL_FOLDER)

    sys.exit()

# Map aws accounts to jenkins profiles:
##########################################################
def getJenkinsProfile(awsAccountIdOrAlias):

    myDict = {
        '111111111111': 'jenkins-on-xxx-shared-prod',
        '111111111111': 'jenkins-on-xxx-od-prod',
        '111111111111': 'jenkins-on-xxx-od-nonprod',
        '111111111111': 'jenkins-on-xxx-od-poc',
        '111111111111': 'jenkins-on-xxx-sports-poc',
        '111111111111': 'jenkins-on-xxx-knicks-prod',
        '111111111111': 'jenkins-on-xxx-knicks-nonprod',
        '111111111111': 'jenkins-on-xxx-rangers-prod',
        '111111111111': 'jenkins-on-xxx-rangers-nonprod',
        'xxx-shared-prod': 'jenkins-on-xxx-shared-prod',
        'xxx-od-prod': 'jenkins-on-xxx-od-prod',
        'xxx-od-nonprod': 'jenkins-on-xxx-od-nonprod',
        'xxx-od-poc': 'jenkins-on-xxx-od-poc',
        'xxx-sports-poc': 'jenkins-on-xxx-sports-poc',
        'xxx-knicks-prod': 'jenkins-on-xxx-knicks-prod',
        'xxx-knicks-nonprod': 'jenkins-on-xxx-knicks-nonprod',
        'xxx-rangers-prod': 'jenkins-on-xxx-rangers-prod',
        'xxx-rangers-nonprod': 'jenkins-on-xxx-rangers-nonprod'
    }

    return myDict.get(awsAccountIdOrAlias)

def getAWSAccountAliasAndID(awsAccountIdOrAlias):

    myDict = {
        '111111111111': 'xxx-shared-prod (111111111111)',
        '111111111111': 'xxx-od-prod (111111111111)',
        '111111111111': 'xxx-od-nonprod (111111111111)',
        '111111111111': 'xxx-od-poc (111111111111)',
        '111111111111': 'xxx-sports-poc (111111111111)',
        '111111111111': 'xxx-knicks-prod (111111111111)',
        '111111111111': 'xxx-knicks-nonprod (111111111111)',
        '111111111111': 'xxx-rangers-prod (111111111111)',
        '111111111111': 'xxx-rangers-nonprod (111111111111)',
        'xxx-shared-prod': 'xxx-shared-prod (111111111111)',
        'xxx-od-prod': 'xxx-od-prod (111111111111)',
        'xxx-od-nonprod': 'xxx-od-nonprod (111111111111)',
        'xxx-od-poc': 'xxx-od-poc (111111111111)',
        'xxx-sports-poc': 'xxx-sports-poc (111111111111)',
        'xxx-knicks-prod': 'xxx-knicks-prod (111111111111)',
        'xxx-knicks-nonprod': 'xxx-knicks-nonprod (111111111111)',
        'xxx-rangers-prod': 'xxx-rangers-prod (111111111111)',
        'xxx-rangers-nonprod': 'xxx-rangers-nonprod (111111111111)'
    }

    return myDict.get(awsAccountIdOrAlias)

def getSSMSecret(ssmSecretAWSAccountAliasOrID, ssmSecretAWSRegion, ssmSecretName):

    if ssmSecretAWSAccountAliasOrID == "":
        # using the profile set on AWS_PROFILE, when running this app from local machine: AWS_PROFILE=myprofile ./wp-migration.py
        # if running as lambda, it will use the attached role
        ssm = boto3.client('ssm', region_name=ssmSecretAWSRegion)
        parameter = ssm.get_parameter(Name=ssmSecretName, WithDecryption=True)
        ssmSecretValue = parameter['Parameter']['Value']

        return ssmSecretValue

    else:

        boto3.setup_default_session(profile_name=getJenkinsProfile(ssmSecretAWSAccountAliasOrID), region_name=ssmSecretAWSRegion)
        ssm = boto3.client('ssm', region_name=ssmSecretAWSRegion)
        parameter = ssm.get_parameter(Name=ssmSecretName, WithDecryption=True)
        ssmSecretValue = parameter['Parameter']['Value']

        return ssmSecretValue

def runLocalCommand(commandDescription, command):

    # command = "ls -lah"
    # command = ["echo 'hi'", "yum update"]

    runLocalCommandResultDict = {}

    try:

        output = subprocess.check_output(command,stderr=subprocess.STDOUT, shell=True, timeout=20, universal_newlines=True)

        runLocalCommandResultDict["executedCommand"] = command
        runLocalCommandResultDict["commandReturnCode"] = 0
        runLocalCommandResultDict["commandStdout"] = output
        runLocalCommandResultDict["commandStderr"] = ""

        logger.info("===================================================================================")
        logger.info("LOCAL COMMAND DESCRIPTION: {}".format(commandDescription))
        logger.info("EXECUTING LOCAL COMMAND: {}".format(command))
        logger.info("COMMAND EXIT STATUS: {} (SUCCESS!)".format("0"))
        logger.info("COMMAND STDOUT:")
        logger.info(output)
        logger.info("===================================================================================")

        return runLocalCommandResultDict

    except subprocess.CalledProcessError as err:

        runLocalCommandResultDict["executedCommand"] = command
        runLocalCommandResultDict["commandReturnCode"] = err.returncode
        runLocalCommandResultDict["commandStdout"] = ""
        runLocalCommandResultDict["commandStderr"] = err.output

        logger.info("===================================================================================")
        logger.info("LOCAL COMMAND DESCRIPTION: {}".format(commandDescription))
        logger.info("EXECUTING LOCAL COMMAND: {}".format(command))
        logger.info("COMMAND EXIT STATUS: {} (FAIL!)".format(err.returncode))
        logger.info("COMMAND STDERROR:")
        logger.info(err.output)
        logger.info("===================================================================================")

        return runLocalCommandResultDict

def runLocalCommandWithPipes(commandDescription, command):

    # command = "ls -lah"
    # command = ["echo 'hi'", "yum update"]
    runLocalCommandResultDict = {}
    decodedResultList = []

    output = ""
    
    try:
    
        commandResult = subprocess.Popen( command, shell=True,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        commandResultList = commandResult.stdout.readlines(-1)

        runLocalCommandResultDict["executedCommand"] = command
        runLocalCommandResultDict["commandReturnCode"] = 0
        runLocalCommandResultDict["commandStderr"] = ""

        for resultLine in commandResultList:
            decodedResultList.append(resultLine.decode("utf-8").rstrip('\r\n'))

        runLocalCommandResultDict["commandStdout"] = decodedResultList

        logger.info("===================================================================================")
        logger.info("LOCAL COMMAND DESCRIPTION: {}".format(commandDescription))
        logger.info("EXECUTING LOCAL COMMAND: {}".format(repr(command)))
        logger.info("COMMAND EXIT STATUS: {} (SUCCESS!)".format("0"))
        logger.info("COMMAND STDOUT:")
        logger.info(output)
        logger.info("===================================================================================")

        return runLocalCommandResultDict

    except subprocess.CalledProcessError as err:

        runLocalCommandResultDict["executedCommand"] = command
        runLocalCommandResultDict["commandReturnCode"] = err.returncode
        runLocalCommandResultDict["commandStdout"] = ""
        runLocalCommandResultDict["commandStderr"] = err.output

        logger.info("===================================================================================")
        logger.info("LOCAL COMMAND DESCRIPTION: {}".format(commandDescription))
        logger.info("EXECUTING LOCAL COMMAND: {}".format(repr(command)))
        logger.info("COMMAND EXIT STATUS: {} (FAIL!)".format(err.returncode))
        logger.info("COMMAND STDERROR:")
        logger.info(err.output)
        logger.info("===================================================================================")

        return runLocalCommandResultDict

def get_user_yes_no_reply(question):
    reply = str(input(question+' (y/n): ')).lower().strip()
    if reply[0] == 'y':
        return True
    if reply[0] == 'n':
        return False
    else:
        return get_user_yes_no_reply(question)

def buildWebsiteTerraformEnv(
    WPENGINE_INSTALL_NAME,
    PS_GITHUB_USERNAME,
    GITHUB_TOKEN,
    TFVARS_AWS_PROFILE,
    TFVARS_PHASE,
    TFVARS_ENV,
    TFVARS_PROJECT,
    TFVARS_AWS_REGION,
    TFVARS_AWS_AZS,
    TFVARS_WP_REPO_NAME,
    TFVARS_WP_DB_NAME,
    TFVARS_WP_DB_USERNAME,
    TFVARS_WP_DB_PASSWORD,
    TFVARS_WP_DB_CHARSET,
    TFVARS_WP_DB_COLLATE,
    TFVARS_WP_TABLE_PREFIX,
    TFVARS_WP_DOMAIN,
    TFVARS_CONTAINER_TAG,
    TFVARS_LAMBDA_ENV,
    TFVARS_INSTALL_PRODUCTION,
    TFVARS_STATE_FILE_BUCKET,
    TFVARS_STATE_FILE_ACCOUNT,
    TFVARS_STATE_FILE_REGION
):

    # get git password:
    # The Git repository to clone
    repo_local_dir = os.path.dirname(os.path.abspath( __file__ )) + '/terraform-configs/msg-wordpress-terraform-' + WPENGINE_INSTALL_NAME
    GITHUB_REPO_URL = 'https://{}:{}@github.com/MSGTech/msg-wordpress-terraform.git'.format(PS_GITHUB_USERNAME,GITHUB_TOKEN)

    # Clone the remote Git repository
    ##########################################

    # first remove previous rpo downloads
    command = 'rm -rf {}'.format(repo_local_dir)
    runLocalCommand("removing previous terraform repo content if any...", command)

    # clone
    command = '/usr/bin/git clone {} {}'.format(GITHUB_REPO_URL, repo_local_dir)
    runLocalCommandResult = runLocalCommand("Cloning the repo: https://github.com/MSGTech/msg-wordpress-terraform.git", command)

    # set up terraform.tfvars file
    ##########################################
    tfvarsContent = """aws_profile = "{}"

phase = "{}"

env = "{}"

project = "{}"

aws_region = "{}"

availability_zones = {}

wordpress_repo_name = "{}"

wp_db_name = "{}"

wp_db_username = "{}"

wp_db_password = "{}"

wp_db_charset = "{}"

wp_db_collate = "{}"

wp_table_prefix = "{}"

domain = "{}"

state-file-bucket = "{}"

container_tag = "{}"

lambda_env = "{}"

install_production = "{}"
""".format(
    TFVARS_AWS_PROFILE,
    TFVARS_PHASE,
    TFVARS_ENV,
    TFVARS_PROJECT,
    TFVARS_AWS_REGION,
    TFVARS_AWS_AZS,
    TFVARS_WP_REPO_NAME,
    TFVARS_WP_DB_NAME,
    TFVARS_WP_DB_USERNAME,
    TFVARS_WP_DB_PASSWORD,
    TFVARS_WP_DB_CHARSET,
    TFVARS_WP_DB_COLLATE,
    TFVARS_WP_TABLE_PREFIX,
    TFVARS_WP_DOMAIN,
    TFVARS_STATE_FILE_BUCKET,
    TFVARS_CONTAINER_TAG,
    TFVARS_LAMBDA_ENV,
    TFVARS_INSTALL_PRODUCTION
)

    logger.info("TF Vars File Content:\n{}".format(tfvarsContent))

    tfvarsHiddenFilePath = repo_local_dir + '/1-env/.terraform.tfvars.' + TFVARS_PROJECT + '.' + TFVARS_ENV

    with open(tfvarsHiddenFilePath, "w") as f:
        f.write(tfvarsContent)

    print(tfvarsHiddenFilePath)

    with open(tfvarsHiddenFilePath, "r") as f:
        data = f.read()

    print(data)

    # create soft link, first remove any previous soft links
    ###########################
    tfvarsLinkedFile = repo_local_dir + "/1-env/terraform.tfvars"

    if os.path.isfile(tfvarsLinkedFile):
        command = 'rm {}/1-env/terraform.tfvars'.format(repo_local_dir)
        runLocalCommand("removing previous soft link: {}".format(tfvarsLinkedFile), command)

    command = 'ln -s {} {}'.format(tfvarsHiddenFilePath, tfvarsLinkedFile)
    runLocalCommand("soft linking {} to {}".format(tfvarsHiddenFilePath, tfvarsLinkedFile), command)

    # TERRAFORM
    ###########################

    stateFileFolder = ""
    stateFileS3Key = ""
    tf1ENVDir = repo_local_dir + "/1-env"

    if TFVARS_PHASE.upper() == "PREPROD":

        # make sure the state file is set up 
        ###########################
        boto3.setup_default_session(profile_name=getJenkinsProfile(TFVARS_STATE_FILE_ACCOUNT), region_name=TFVARS_STATE_FILE_REGION)
        stateFileFolder = "preprod/{}-{}/".format(TFVARS_PROJECT, TFVARS_ENV)
        stateFileS3Key = stateFileFolder + "terraform.tfstate"
        create_s3_folder(TFVARS_STATE_FILE_BUCKET, stateFileFolder)

    if TFVARS_PHASE.upper() == "PROD":

        # make sure the state file is set up 
        ###########################
        boto3.setup_default_session(profile_name=getJenkinsProfile(TFVARS_STATE_FILE_ACCOUNT), region_name=TFVARS_STATE_FILE_REGION)
        stateFileFolder = "prod/{}-{}/".format(TFVARS_PROJECT, TFVARS_ENV)
        stateFileS3Key = stateFileFolder + "terraform.tfstate"
        create_s3_folder(TFVARS_STATE_FILE_BUCKET, stateFileFolder)

    # initialize terraform 
    ###########################

    tfInitCommand = '''cd {} && AWS_PROFILE={} terraform init -backend-config "bucket={}" -backend-config "key={}" -backend-config "region={}"'''.format(
        tf1ENVDir,
        getJenkinsProfile(TFVARS_STATE_FILE_ACCOUNT),
        TFVARS_STATE_FILE_BUCKET,
        stateFileS3Key,
        TFVARS_STATE_FILE_REGION
    )

    logger.info(
        "Initializing terraform...with the following parameters:\naws account: {}\nbuket: {}\nkey: {}\nregion: {}".format(
            getAWSAccountAliasAndID(TFVARS_STATE_FILE_ACCOUNT),
            TFVARS_STATE_FILE_BUCKET,
            stateFileS3Key,
            TFVARS_STATE_FILE_REGION
        )
    )

    # we are using os.system because terraform is not running properly with runLocalCommand function
    os.system(tfInitCommand)

    # runLocalCommand(
    #     "Initializing terraform...with the following parameters:\naws account: {}\nbuket: {}\nkey: {}\nregion: {}".format(
    #         TFVARS_STATE_FILE_ACCOUNT,
    #         TFVARS_STATE_FILE_BUCKET,
    #         stateFileS3Key,
    #         TFVARS_STATE_FILE_REGION
    #     ),
    #     tfInitCommand
    # )

    # run terraform plan
    ###########################
    
    tfPlanCommand = '''cd {} && AWS_PROFILE={} terraform plan'''.format(
        tf1ENVDir,
        getJenkinsProfile(TFVARS_STATE_FILE_ACCOUNT)
    )

    logger.info("Running terraform plan:\nCOMMAND: {}".format(tfPlanCommand))

    # we are using os.system because terraform is not running properly with runLocalCommand function
    os.system(tfPlanCommand)

    # runLocalCommand(
    #     "Running terraform Plan...",
    #     tfPlanCommand
    # )

    userReply = get_user_yes_no_reply("Apply Terraform?")

    if userReply == True:

        # run terraform apply
        ###########################
        tfApplyCommand = '''cd {} && AWS_PROFILE={} terraform apply -auto-approve'''.format(
            tf1ENVDir,
            getJenkinsProfile(TFVARS_STATE_FILE_ACCOUNT)
        )

        logger.info("Running terraform apply:\nCOMMAND: {}".format(tfApplyCommand))

        os.system(tfApplyCommand)

        # runLocalCommand(
        #     "Running terraform Plan...",
        #     tfApplyCommand
        # )

        #update migration status:
        update_migration_status(
            WPENGINE_INSTALL_NAME,
            "websiteTerraformStatus",
            "COMPLETE"
        )

        # # remove the terraform folder after done:
        # command = 'rm -rf {}'.format(repo_local_dir)
        # runLocalCommand("removing local terraform file...", command)

    else:

        # # remove the terraform folder after done:
        # command = 'rm -rf {}'.format(repo_local_dir)
        # runLocalCommand("removing local terraform file...", command)

        logger.info("Existing script... user replied no to applying terraform.")

        sys.exit()

def restoreWPDBOnAWSEnv(
    WPENGINE_INSTALL_NAME,
    wpBastionKeyName,
    wpBastionEC2User,
    wpBastionPublicIp,
    WP_BASTION_WEBSITE_CONTENT_DIR,
    jenkinsInstanceID, 
    jenkinsAWSProfile, 
    jenkinsECsRegion, 
    jenkinsSSHClient,
    WPENGINE_CERT_URL,
    WPENGINE_CERT_LOCAL_PATH,
    MSG_WP_RDS_HOST_END_POINT,
    MSG_WP_RDS_DB_USERNAME,
    MSG_WP_RDS_DB_PASSWORD
):

    # get wpe db credentials from wp-config.php

    # CONNECT TO WPENGINE SFTP HOST:
    sftpConnection = openSFTPConToWpe(
        WPENGINE_SFTP_HOST, 
        int(WPENGINE_SFTP_HOST_PORT), 
        WPENGINE_SFTP_USERNAME, 
        WPENGINE_SFTP_PASSWORD
    )
   
    if sftpConnection == 'conn_error':
        logger.error('Failed to connect FTP Server: {} ... EXITING!'.format(WPENGINE_SFTP_HOST))
        sys.exit()

    if sftpConnection == 'auth_error':
        logger.error('Incorrect username or password for: {} ... EXITING!'.format(WPENGINE_SFTP_HOST))
        sys.exit()

    sftpConnection.get("/wp-config.php", "wp-config.php")

    # WPE_DB_USERNAME=`cat wp-config.php | grep DB_USER | cut -d \' -f 4`
    command = """cat wp-config.php | grep DB_USER | cut -d \\' -f 4"""
    commandResult = runLocalCommand("Extract WPE DB USER from wp-config.php", command)
    WPE_DB_USERNAME = ""
    if commandResult["commandReturnCode"] == 0:
        WPE_DB_USERNAME = commandResult["commandStdout"].strip()
    else:
        logger.error("Unable to retrieve WPE DB USER from wp-config.php")

    # WPE_DB_PASSWORD=`cat wp-config.php | grep DB_PASSWORD | cut -d \' -f 4`
    command = """cat wp-config.php | grep DB_PASSWORD | cut -d \\' -f 4"""
    commandResult = runLocalCommand("Extract WPE DB PASSWORD from wp-config.php", command)
    WPE_DB_PASSWORD = ""
    if commandResult["commandReturnCode"] == 0:
        WPE_DB_PASSWORD = commandResult["commandStdout"].strip()
    else:
        logger.error("Unable to retrieve WPE DB PASSWORD from wp-config.php")

    # download wpe cert on jenkins, because jenkin is whitlisted by wpe:
    # curl https://storage.googleapis.com/wpengine-public-assets/certificates/wpengine_root_ca.pem -o /tmp/wpengine_root_ca.pem
    jenkinsCommand = "curl {} -o {}".format(
        WPENGINE_CERT_URL, 
        WPENGINE_CERT_LOCAL_PATH
    )
    commandResult = runCommandOnEc2(
        "download wpe cert to jenkins", 
        jenkinsCommand,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    jenkinsCommand = "mysqldump -u {} -p{} -h {} -P13306 --ssl-ca={} {} > /tmp/{}.sql".format(
        WPE_DB_USERNAME, 
        WPE_DB_PASSWORD, 
        WPENGINE_SFTP_HOST, 
        WPENGINE_CERT_LOCAL_PATH, 
        WPE_DB_NAME, 
        WPE_DB_NAME
    )
    commandResult = runCommandOnEc2(
        "take a backup of the wpe db", 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # move db backup from jenkins to wp bastion non prod:
    jenkinsCommand = "scp -i ~/.ssh/{} /tmp/{}.sql {}@{}:/tmp/".format(
        wpBastionKeyName, 
        WPE_DB_NAME, 
        wpBastionEC2User, 
        wpBastionPublicIp
    )
    commandResult = runCommandOnEc2(
        "move db backup from jenkins to wp bastion non prod.", 
        jenkinsCommand,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # Make sure the db has been created: mysql> show databases;
    jenkinsCommand = """ssh -i ~/.ssh/{} {}@{} 'mysql -h {} -u {} -p{} -e "show databases;"'""".format(
        wpBastionKeyName, 
        wpBastionEC2User, 
        wpBastionPublicIp, 
        MSG_WP_RDS_HOST_END_POINT, 
        MSG_WP_RDS_DB_USERNAME, 
        MSG_WP_RDS_DB_PASSWORD
    )
    commandResult = runCommandOnEc2(
        "Make sure the db has been created.", 
        jenkinsCommand,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    if MSG_WP_RDS_DB_NAME in commandResult['std_out']:
        logger.info(
            "The database: {} already exists on the RDS: {}, no need to create it.".format(
                MSG_WP_RDS_DB_NAME, 
                MSG_WP_RDS_HOST_END_POINT
            )
        )

        # since the aws db already exists offer to back it up:

        userReply = get_user_yes_no_reply("Before restoring, Backup aws wp db: {} to S3: {}?".format(MSG_WP_RDS_DB_NAME, MSG_WP_BACKUP_S3_BUCKET_NAME))
    
        if userReply == True:

            backupAWSWPDBToS3(
                TFVARS_WP_DB_USERNAME,
                TFVARS_WP_DB_PASSWORD,
                MSG_WP_RDS_HOST_END_POINT,
                TFVARS_WP_DB_NAME,
                jenkinsCommand, 
                jenkinsInstanceID,
                jenkinsAWSProfile,
                jenkinsECsRegion,
                jenkinsSSHClient,
                wpBastionKeyName,
                wpBastionEC2User, 
                wpBastionPublicIp
            )

        # since the db already exists, just restore db:
        jenkinsCommand = '''ssh -i ~/.ssh/{} {}@{} "mysql -u {} -p{} -h {} -D {} < /tmp/{}.sql"'''.format(
            wpBastionKeyName, 
            wpBastionEC2User, 
            wpBastionPublicIp, 
            MSG_WP_RDS_DB_USERNAME, 
            MSG_WP_RDS_DB_PASSWORD, 
            MSG_WP_RDS_HOST_END_POINT, 
            MSG_WP_RDS_DB_NAME, 
            WPE_DB_NAME
        )
        commandResult = runCommandOnEc2(
            "restore db from backup.", 
            jenkinsCommand, 
            jenkinsInstanceID, 
            jenkinsAWSProfile, 
            jenkinsECsRegion, 
            jenkinsSSHClient
        )
        logger.info(
            "The database: {} has been restored on the RDS: {}".format(
                MSG_WP_RDS_DB_NAME, 
                MSG_WP_RDS_HOST_END_POINT
            )
        )

        #update migration status:
        update_migration_status(
            WPENGINE_INSTALL_NAME,
            "dbRestoreStatus",
            "COMPLETE"
        )

    else:

        jenkinsCommand = """ssh -i ~/.ssh/{} {}@{} 'mysql -h {} -u {} -p{} -e "create database {};"'""".format(
            wpBastionKeyName, 
            wpBastionEC2User, 
            wpBastionPublicIp, 
            MSG_WP_RDS_HOST_END_POINT, 
            MSG_WP_RDS_DB_USERNAME, 
            MSG_WP_RDS_DB_PASSWORD, 
            MSG_WP_RDS_DB_NAME
        )
        commandResult = runCommandOnEc2(
            "create the db on the non prod rds.", 
            jenkinsCommand, 
            jenkinsInstanceID, 
            jenkinsAWSProfile, 
            jenkinsECsRegion, 
            jenkinsSSHClient
        )
        logger.info(
            "The database: {} has been created on the RDS: {}".format(
                MSG_WP_RDS_DB_NAME, 
                MSG_WP_RDS_HOST_END_POINT
            )
        )

        # restore db from backup
        # mysql -u wordpressuser -pqhRQYdCJbWqHGYXTsijqh -h terraform-20190917153953384600000001.cutudqluqvmh.us-east-1.rds.amazonaws.com -D footest < /tmp/wp_msghrportaldev.sql
        jenkinsCommand = '''ssh -i ~/.ssh/{} {}@{} "mysql -u {} -p{} -h {} -D {} < /tmp/{}.sql"'''.format(
            wpBastionKeyName, 
            wpBastionEC2User, 
            wpBastionPublicIp, 
            MSG_WP_RDS_DB_USERNAME, 
            MSG_WP_RDS_DB_PASSWORD, 
            MSG_WP_RDS_HOST_END_POINT, 
            MSG_WP_RDS_DB_NAME, 
            WPE_DB_NAME
        )
        commandResult = runCommandOnEc2(
            "restore db from backup.", 
            jenkinsCommand, 
            jenkinsInstanceID, 
            jenkinsAWSProfile, 
            jenkinsECsRegion, 
            jenkinsSSHClient
        )
        logger.info(
            "The database: {} has been restored on the RDS: {}".format(
                MSG_WP_RDS_DB_NAME, 
                MSG_WP_RDS_HOST_END_POINT
            )
        )

        #update migration status:
        update_migration_status(
            WPENGINE_INSTALL_NAME,
            "dbRestoreStatus",
            "COMPLETE"
        )

def backupWPEWPDBToS3(
    WPENGINE_INSTALL_NAME,
    jenkinsInstanceID, 
    jenkinsAWSProfile, 
    jenkinsECsRegion, 
    jenkinsSSHClient,
    WPENGINE_CERT_URL,
    WPENGINE_CERT_LOCAL_PATH,
    MSG_WP_RDS_HOST_END_POINT,
    MSG_WP_RDS_DB_USERNAME,
    MSG_WP_RDS_DB_PASSWORD,
    WPE_DB_NAME
):

    # get wpe db credentials from wp-config.php

    # CONNECT TO WPENGINE SFTP HOST:
    sftpConnection = openSFTPConToWpe(
        WPENGINE_SFTP_HOST, 
        int(WPENGINE_SFTP_HOST_PORT), 
        WPENGINE_SFTP_USERNAME, 
        WPENGINE_SFTP_PASSWORD
    )
   
    if sftpConnection == 'conn_error':
        logger.error('Failed to connect FTP Server: {} ... EXITING!'.format(WPENGINE_SFTP_HOST))
        sys.exit()

    if sftpConnection == 'auth_error':
        logger.error('Incorrect username or password for: {} ... EXITING!'.format(WPENGINE_SFTP_HOST))
        sys.exit()

    sftpConnection.get("/wp-config.php", "wp-config.php")

    # WPE_DB_USERNAME=`cat wp-config.php | grep DB_USER | cut -d \' -f 4`
    command = """cat wp-config.php | grep DB_USER | cut -d \\' -f 4"""
    commandResult = runLocalCommand("Extract WPE DB USER from wp-config.php", command)
    WPE_DB_USERNAME = ""
    if commandResult["commandReturnCode"] == 0:
        WPE_DB_USERNAME = commandResult["commandStdout"].strip()
    else:
        logger.error("Unable to retrieve WPE DB USER from wp-config.php")

    # WPE_DB_PASSWORD=`cat wp-config.php | grep DB_PASSWORD | cut -d \' -f 4`
    command = """cat wp-config.php | grep DB_PASSWORD | cut -d \\' -f 4"""
    commandResult = runLocalCommand("Extract WPE DB PASSWORD from wp-config.php", command)
    WPE_DB_PASSWORD = ""
    if commandResult["commandReturnCode"] == 0:
        WPE_DB_PASSWORD = commandResult["commandStdout"].strip()
    else:
        logger.error("Unable to retrieve WPE DB PASSWORD from wp-config.php")

    # download wpe cert on jenkins, because jenkin is whitlisted by wpe:
    # curl https://storage.googleapis.com/wpengine-public-assets/certificates/wpengine_root_ca.pem -o /tmp/wpengine_root_ca.pem
    jenkinsCommand = "curl {} -o {}".format(
        WPENGINE_CERT_URL, 
        WPENGINE_CERT_LOCAL_PATH
    )
    commandResult = runCommandOnEc2(
        "download wpe cert to jenkins", 
        jenkinsCommand,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # take a backup of the wpe db from jenkins becuse it is whitlisted by their network:
    # mysqldump -u msghrportaldev -pcUBNmP6HNhfb97Gfz5QK -h msghrportaldev.sftp.wpengine.com -P13306 --ssl-ca=/tmp/wpengine_root_ca.pem wp_msghrportaldev > /tmp/wp_msghrportaldev.sql
    jenkinsCommand = "mysqldump -u {} -p{} -h {} -P13306 --ssl-ca={} {} > /tmp/{}.sql".format(
        WPE_DB_USERNAME, 
        WPE_DB_PASSWORD, 
        WPENGINE_SFTP_HOST, 
        WPENGINE_CERT_LOCAL_PATH, 
        WPE_DB_NAME, 
        WPE_DB_NAME
    )
    commandResult = runCommandOnEc2(
        "take a backup of the wpe db", 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # move the backup file from jenkins to s3
    # aws s3 cp s3://my-source-bucket s3://my-destination-bucket --profile userprofile-on-the-destination-account

    # prepare a timestamp:
    start_time = datetime.now()
    eastern = timezone('US/Eastern')
    localized_backup_timestamp = eastern.localize(start_time)
    backup_timestamp = localized_backup_timestamp.strftime("%a-%d-%b-%Y[%H-%M-%S-%Z%z]")

    jenkinsCommand = """aws s3 cp /tmp/{}.sql s3://{}/{}/wpdb/{}-{}.sql --profile {}""".format(
        WPE_DB_NAME,
        MSG_WP_BACKUP_S3_BUCKET_NAME,
        WPENGINE_INSTALL_NAME,
        WPENGINE_INSTALL_NAME,
        backup_timestamp,
        getJenkinsProfile(MSG_WP_BACKUP_S3_BUCKET_ACCOUNT_ID_OR_ALIAS)
    )
    commandResult = runCommandOnEc2(
        "Copy backup of the wpe db install: {} to s3: {}".format(WPENGINE_INSTALL_NAME, MSG_WP_BACKUP_S3_BUCKET_NAME), 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    #update migration status:
    update_migration_status(
        WPENGINE_INSTALL_NAME,
        "backupWPEWPDBToS3Status",
        "COMPLETE"
    )

def backupAWSWPDBToS3(
    TFVARS_WP_DB_USERNAME,
    TFVARS_WP_DB_PASSWORD,
    MSG_WP_RDS_HOST_END_POINT,
    TFVARS_WP_DB_NAME,
    jenkinsCommand, 
    jenkinsInstanceID,
    jenkinsAWSProfile,
    jenkinsECsRegion,
    jenkinsSSHClient,
    wpBastionKeyName,
    wpBastionEC2User, 
    wpBastionPublicIp
):

    # take a backup of the aws rds wp db from the bastion:
    # mysqldump -u wordpressuser -pcUBNmP6HNhfb97Gfz5QK -h terraform-20190917153953384600000001.cutudqluqvmh.us-east-1.rds.amazonaws.com msgmarqqa > /tmp/msgmarqqa.sql
    jenkinsCommand = '''ssh -i ~/.ssh/{} {}@{} "mysqldump -u {} -p{} -h {} {} > /tmp/{}.sql"'''.format(
        wpBastionKeyName, 
        wpBastionEC2User, 
        wpBastionPublicIp,
        TFVARS_WP_DB_USERNAME, 
        TFVARS_WP_DB_PASSWORD, 
        MSG_WP_RDS_HOST_END_POINT,
        TFVARS_WP_DB_NAME, 
        TFVARS_WP_DB_NAME
    )
    commandResult = runCommandOnEc2(
        "Taking a backup of the aws wp db: {}".format(TFVARS_WP_DB_NAME), 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # move files from bastion to jenkins
    jenkinsCommand = "scp -i ~/.ssh/{} {}@{}:/tmp/{}.sql /tmp/{}.sql".format(
        wpBastionKeyName,
        wpBastionEC2User, 
        wpBastionPublicIp,
        TFVARS_WP_DB_NAME,
        TFVARS_WP_DB_NAME
    )
    commandResult = runCommandOnEc2(
        "move aws wp db backup file: {} from bastion to jenkins".format(TFVARS_WP_DB_NAME), 
        jenkinsCommand,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # move the backup file from jenkins to s3
    # aws s3 cp s3://my-source-bucket s3://my-destination-bucket --profile userprofile-on-the-destination-account

    # prepare a timestamp:
    start_time = datetime.now()
    eastern = timezone('US/Eastern')
    localized_backup_timestamp = eastern.localize(start_time)
    backup_timestamp = localized_backup_timestamp.strftime("%a-%d-%b-%Y[%H-%M-%S-%Z%z]")

    jenkinsCommand = """aws s3 cp /tmp/{}.sql s3://{}/{}/wpdb/{}-{}.sql --profile {}""".format(
        TFVARS_WP_DB_NAME,
        MSG_WP_BACKUP_S3_BUCKET_NAME,
        TFVARS_WP_DB_NAME,
        TFVARS_WP_DB_NAME,
        backup_timestamp,
        getJenkinsProfile(MSG_WP_BACKUP_S3_BUCKET_ACCOUNT_ID_OR_ALIAS)
    )

    commandResult = runCommandOnEc2(
        "Copy backup of the aws wp db: {} to s3: {}".format(TFVARS_WP_DB_NAME, MSG_WP_BACKUP_S3_BUCKET_NAME), 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

def updateWebsiteURLInWPDB(
    WPENGINE_INSTALL_NAME,
    TFVARS_WP_DOMAIN,
    WEBSITE_DOMAIN_OLD,
    jenkinsInstanceID, 
    jenkinsAWSProfile, 
    jenkinsECsRegion, 
    jenkinsSSHClient,
    wpBastionKeyName,
    wpBastionEC2User, 
    wpBastionPublicIp
):

    logger.info(
        "Updating the website domain from old URL: {} to new URL: {}".format(
            WEBSITE_DOMAIN_OLD,
            TFVARS_WP_DOMAIN
        )
    )

    buildUpdateWpWebsiteURLFiles()

    fileBashData = ""
    logger.info("File content: /tmp/update_wp_site_url.sh")
    with open("/tmp/update_wp_site_url.sh") as f:
        fileBashData = f.read()
    logger.info(fileBashData)

    fileSqlData = ""
    logger.info("File content: /tmp/update_wp_site_url.sql")
    with open("/tmp/update_wp_site_url.sql") as f:
        fileSqlData = f.read()
    logger.info(fileSqlData)

    # move the script files from here to S3:
    boto3.setup_default_session(profile_name='jenkins-on-msge-shared-prod', region_name="us-east-1")
    upload_file_to_s3("msg-devops-scripts", "update_wp_site_url.sh", "/tmp/update_wp_site_url.sh")

    boto3.setup_default_session(profile_name='jenkins-on-msge-shared-prod', region_name="us-east-1")
    upload_file_to_s3("msg-devops-scripts", "update_wp_site_url.sql", "/tmp/update_wp_site_url.sql")

    # download files from s3 to jenkins
    jenkinsCommand = "aws s3 cp s3://msg-devops-scripts/update_wp_site_url.sh /tmp/ --profile jenkins-on-msge-shared-prod"

    commandResult = runCommandOnEc2(
        "Downloding update_wp_site_url.sh file to jenkins", 
        jenkinsCommand,
        jenkinsInstanceID,
        jenkinsAWSProfile,
        jenkinsECsRegion,
        jenkinsSSHClient
    )

    jenkinsCommand = "aws s3 cp s3://msg-devops-scripts/update_wp_site_url.sql /tmp/ --profile jenkins-on-msge-shared-prod"

    commandResult = runCommandOnEc2(
        "Downloding update_wp_site_url.sql file to jenkins", 
        jenkinsCommand,
        jenkinsInstanceID,
        jenkinsAWSProfile,
        jenkinsECsRegion,
        jenkinsSSHClient
    )

    # move files from jenkins to bastion
    jenkinsCommand = "scp -i ~/.ssh/{} /tmp/update_wp_site_url.sh {}@{}:/tmp/".format(
        wpBastionKeyName,
        wpBastionEC2User, 
        wpBastionPublicIp
    )
    commandResult = runCommandOnEc2(
        "move update_wp_site_url.sh file from jenkins to wp bastion", 
        jenkinsCommand,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    jenkinsCommand = "scp -i ~/.ssh/{} /tmp/update_wp_site_url.sql {}@{}:/tmp/".format(
        wpBastionKeyName,
        wpBastionEC2User, 
        wpBastionPublicIp
    )
    commandResult = runCommandOnEc2(
        "move update_wp_site_url.sql file from jenkins to wp bastion", 
        jenkinsCommand,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # set file permissions:
    jenkinsCommand = '''ssh -i ~/.ssh/{} {}@{} "sudo chmod a+x /tmp/update_wp_site_url.sh"'''.format(
        wpBastionKeyName, 
        wpBastionEC2User, 
        wpBastionPublicIp
    )
    commandResult = runCommandOnEc2(
        "Making the script file /tmp/update_wp_site_url.sh executable.", 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # run script:
    jenkinsCommand = '''ssh -i ~/.ssh/{} {}@{} "bash /tmp/update_wp_site_url.sh"'''.format(
        wpBastionKeyName, 
        wpBastionEC2User, 
        wpBastionPublicIp
    )
    commandResult = runCommandOnEc2(
        "running the script file /tmp/update_wp_site_url.sh", 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    update_migration_status(
        WPENGINE_INSTALL_NAME,
        "updateWebsiteURLinWPDBStatus",
        "COMPLETE"
    )

def pointDomainToAlb(
    WEBSITE_ALB_ARN,
    AWS_ACCOUNT_HOSTING_THE_DOMAIN,
    AWS_REGION_HOSTING_THE_DOMAIN,
    WEBSITE_DOMAIN,
    WEBSITE_SUB_DOMAIN,
    WPENGINE_INSTALL_NAME
):

    # get alb hosted zone id and dnsname:

    boto3.setup_default_session(profile_name='jenkins-on-msge-od-nonprod', region_name="us-east-1")
    alb = boto3.client('elbv2')
    albResponse = alb.describe_load_balancers()
    albList = albResponse['LoadBalancers']
    albDict = next((item for item in albList if item["LoadBalancerArn"] == WEBSITE_ALB_ARN), None)

    if albDict:
        WEBSITE_ALB_ALIAS_HOSTED_ZONE_ID = albDict["CanonicalHostedZoneId"]
        WEBSITE_ALB_DNS = albDict["DNSName"]
        logger.info("ALB: {} Located!".format(WEBSITE_ALB_ARN))
    else:
        logger.error("ALB: {} was not found! Exiting...".format(WEBSITE_ALB_ARN))
        sys.exit()

    boto3.setup_default_session(
        profile_name=getJenkinsProfile(AWS_ACCOUNT_HOSTING_THE_DOMAIN), 
        region_name=AWS_REGION_HOSTING_THE_DOMAIN
    )
    r53 = boto3.client('route53')
    zones = r53.list_hosted_zones_by_name(DNSName=WEBSITE_DOMAIN)
    WEBSITE_DOMAIN_AWS_ZONE_FORMAT = WEBSITE_DOMAIN + "."
    WEBSITE_ALB_DNS_AWS_ZONE_FORMAT = "dualstack." + WEBSITE_ALB_DNS + "."

    if WEBSITE_SUB_DOMAIN == "":
        WEBSITE_FULL_DOMAIN_AWS_ZONE_FORMAT = WEBSITE_DOMAIN + "."
    else:
        WEBSITE_FULL_DOMAIN_AWS_ZONE_FORMAT = WEBSITE_SUB_DOMAIN + "." + WEBSITE_DOMAIN + "."

    FOUND_HOSTED_ZONE = False
    for zone in zones["HostedZones"]:
        hostedZoneId = zone["Id"]
        hostedZoneName = zone["Name"]
        if hostedZoneName == WEBSITE_DOMAIN_AWS_ZONE_FORMAT:
            FOUND_HOSTED_ZONE = True
            logger.info("Located the hosted zone id: {} on route53 for: {}".format(hostedZoneId, WEBSITE_DOMAIN))
            domainRecordSets = r53.list_resource_record_sets(HostedZoneId=hostedZoneId)
            listOfDictsOfRecordSets = domainRecordSets["ResourceRecordSets"]

            recordSet = next((item for item in listOfDictsOfRecordSets if item["Name"] == WEBSITE_FULL_DOMAIN_AWS_ZONE_FORMAT), None)

            if recordSet:
                # recordSet found no need to create it:
                aRecordValues = recordSet["AliasTarget"]
                currentAlbDnsName = aRecordValues["DNSName"]
                aliasHostedZoneID = aRecordValues["HostedZoneId"]
                
                if WEBSITE_SUB_DOMAIN == "":
                    logger.info("The domain: {} already has an A record, no action required.".format(WEBSITE_DOMAIN))
                else:
                    logger.info("The subdomain: {}.{} already has an A record, no action required.".format(WEBSITE_SUB_DOMAIN, WEBSITE_DOMAIN))

                if currentAlbDnsName == WEBSITE_ALB_DNS_AWS_ZONE_FORMAT:
                    # domain/subdomain pointing to the desired alb, no need to update this A record

                    if WEBSITE_SUB_DOMAIN == "":
                        logger.info("The domain: {} is already pointing to: {}, no action required.".format(WEBSITE_DOMAIN,
                        currentAlbDnsName))
                    else:
                        logger.info("The subdomain: {}.{} is already pointing to: {}, no action required.".format(WEBSITE_SUB_DOMAIN, 
                        WEBSITE_DOMAIN, currentAlbDnsName))

                else:
                    # the domain/subdomain is not pointing to the desired alb, updating the A record now
                    # https://stackoverflow.com/questions/41611921/boto3-python-change-resource-record-sets
                    if WEBSITE_SUB_DOMAIN == "":
                        logger.info("The domain: {} is not pointing to: {}, it currently pointing to: {}".format(WEBSITE_DOMAIN,
                        WEBSITE_ALB_DNS_AWS_ZONE_FORMAT, currentAlbDnsName))

                        logger.info("Pointing the domain: {} to: {}".format(WEBSITE_DOMAIN, WEBSITE_ALB_DNS_AWS_ZONE_FORMAT))

                        response = r53.change_resource_record_sets(
                        HostedZoneId=hostedZoneId,
                        ChangeBatch= {
                                "Comment": "add record A",
                                "Changes": [{
                                    "Action": "UPSERT",
                                    "ResourceRecordSet": {
                                        "Name": WEBSITE_FULL_DOMAIN_AWS_ZONE_FORMAT,
                                        "Type": "A",
                                        "SetIdentifier": "First",
                                        "Weight": 1,
                                        "AliasTarget": {
                                            "HostedZoneId": WEBSITE_ALB_ALIAS_HOSTED_ZONE_ID,
                                            "DNSName": WEBSITE_ALB_DNS_AWS_ZONE_FORMAT,
                                            "EvaluateTargetHealth": False
                                        }
                                    }
                                }]
                        })

                        logger.info("The domain: {} is now pointing to: {}".format(WEBSITE_DOMAIN, 
                        WEBSITE_ALB_DNS_AWS_ZONE_FORMAT))

                    else:

                        logger.info(
                            "The subdomain: {}.{} is not pointing to: {}, it is currently pointing to: {}".format(
                                WEBSITE_SUB_DOMAIN, 
                                WEBSITE_DOMAIN, 
                                WEBSITE_ALB_DNS_AWS_ZONE_FORMAT, 
                                currentAlbDnsName
                            )
                        )

                        logger.info(
                            "Pointing the subdomain: {}.{} to: {}".format(
                                WEBSITE_SUB_DOMAIN, 
                                WEBSITE_DOMAIN, 
                                WEBSITE_ALB_DNS_AWS_ZONE_FORMAT
                            )
                        )

                        response = r53.change_resource_record_sets(
                        HostedZoneId=hostedZoneId,
                        ChangeBatch= {
                                "Comment": "add record A",
                                "Changes": [{
                                    "Action": "UPSERT",
                                    "ResourceRecordSet": {
                                        "Name": WEBSITE_FULL_DOMAIN_AWS_ZONE_FORMAT,
                                        "Type": "A",
                                        "SetIdentifier": "Second",
                                        "Weight": 2,
                                        "AliasTarget": {
                                            "HostedZoneId": WEBSITE_ALB_ALIAS_HOSTED_ZONE_ID,
                                            "DNSName": WEBSITE_ALB_DNS_AWS_ZONE_FORMAT,
                                            "EvaluateTargetHealth": False
                                        }
                                    }
                                }]
                        })

                        logger.info(
                            "The subdomain: {}.{} is now pointing to: {}".format(
                                WEBSITE_SUB_DOMAIN, 
                                WEBSITE_DOMAIN, 
                                WEBSITE_ALB_DNS_AWS_ZONE_FORMAT
                            )
                        )

            else:
                # recordSet not found, create it:
                if WEBSITE_SUB_DOMAIN == "":
                    # must create two records, an A record for domain.com and the subdomain www.domain.com
                    # both pointing to the alb
                    logger.info(
                        "Pointing the domain: {} to the alb: {} by creating an A record on the domain {} and another A record on the subdomain www, both A records will point to the alb: {}".format(
                        WEBSITE_DOMAIN, 
                        WEBSITE_ALB_DNS, 
                        WEBSITE_DOMAIN, 
                        WEBSITE_ALB_DNS
                        )
                    )

                    # first create: mydomain.com A record
                    response = r53.change_resource_record_sets(
                    HostedZoneId=hostedZoneId,
                    ChangeBatch= {
                            "Comment": "add record A",
                            "Changes": [{
                                "Action": "UPSERT",
                                "ResourceRecordSet": {
                                    "Name": WEBSITE_FULL_DOMAIN_AWS_ZONE_FORMAT,
                                    "Type": "A",
                                    "SetIdentifier": "Third",
                                    "Weight": 3,
                                    "AliasTarget": {
                                        "HostedZoneId": WEBSITE_ALB_ALIAS_HOSTED_ZONE_ID,
                                        "DNSName": WEBSITE_ALB_DNS_AWS_ZONE_FORMAT,
                                        "EvaluateTargetHealth": False
                                    }
                                }
                            }]
                    })

                    # then create: www.mydomain.com A record
                    WEBSITE_FULL_DOMAIN_AWS_ZONE_FORMAT = "www." + WEBSITE_FULL_DOMAIN_AWS_ZONE_FORMAT
                    response = r53.change_resource_record_sets(
                    HostedZoneId=hostedZoneId,
                    ChangeBatch= {
                            "Comment": "add record A",
                            "Changes": [{
                                "Action": "UPSERT",
                                "ResourceRecordSet": {
                                    "Name": WEBSITE_FULL_DOMAIN_AWS_ZONE_FORMAT,
                                    "Type": "A",
                                    "SetIdentifier": "Fourth",
                                    "Weight": 4,
                                    "AliasTarget": {
                                        "HostedZoneId": WEBSITE_ALB_ALIAS_HOSTED_ZONE_ID,
                                        "DNSName": WEBSITE_ALB_DNS_AWS_ZONE_FORMAT,
                                        "EvaluateTargetHealth": False
                                    }
                                }
                            }]
                    })

                else:
                    # create only one subdomain and point it to the alb
                    logger.info(
                        "Pointing the subdomain: {}.{} to the alb: {} by creating an A record.".format(
                        WEBSITE_SUB_DOMAIN, WEBSITE_DOMAIN, WEBSITE_ALB_DNS
                        )
                    )

                    response = r53.change_resource_record_sets(
                    HostedZoneId=hostedZoneId,
                    ChangeBatch= {
                            "Comment": "add record A",
                            "Changes": [{
                                "Action": "UPSERT",
                                "ResourceRecordSet": {
                                    "Name": WEBSITE_FULL_DOMAIN_AWS_ZONE_FORMAT,
                                    "Type": "A",
                                    "SetIdentifier": "Fifth",
                                    "Weight": 5,
                                    "AliasTarget": {
                                        "HostedZoneId": WEBSITE_ALB_ALIAS_HOSTED_ZONE_ID,
                                        "DNSName": WEBSITE_ALB_DNS_AWS_ZONE_FORMAT,
                                        "EvaluateTargetHealth": False
                                    }
                                }
                            }]
                    })

                    logger.info(
                        "The subdomain: {}.{} is now pointing to: {}".format(
                            WEBSITE_SUB_DOMAIN, WEBSITE_DOMAIN, WEBSITE_ALB_DNS
                        )
                    )
                
        else:
            continue

    if FOUND_HOSTED_ZONE == False:
        # user must create the hosted zone, because this might involve domain registration:
        logger.info("Website domain not found in route53, please create a hosted zone for {}".format(WEBSITE_DOMAIN))
        logger.info("Exiting!")
        sys.exit()

    #update migration status:
    update_migration_status(
        WPENGINE_INSTALL_NAME,
        "setWebsiteDomainInRoute53Status",
        "COMPLETE"
    )

###########################################################################################
###########################################################################################
###########################################################################################
###########################################################################################
##### PRE MAIN, ENVS NOT REQUIRED
###########################################################################################
###########################################################################################
###########################################################################################
###########################################################################################

# Get Jenkins Credentials
##########################################################

# credentials used here are for the aws account: msge-shared-prod:
# local run: AWS_PROFILE=my-aws-profile ./wp-migration
# lambda: attached role
# all credentials after this point are using the /tmp/credentials file

# get secret
# ssm = boto3.client('ssm', region_name='us-east-1')
# parameter = ssm.get_parameter(Name='jenkins-aws-keys', WithDecryption=True)
# JENKINS_AWS_KEYS=parameter['Parameter']['Value']
JENKINS_AWS_KEYS = getSSMSecret("", 'us-east-1', 'jenkins-aws-keys')

with open("/tmp/credentials", "w") as myfile:
    myfile.write(JENKINS_AWS_KEYS)
os.chmod("/tmp/credentials", stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)

# set env var to point to custom aws cli location: /tmp/credentials
os.environ['AWS_SHARED_CREDENTIALS_FILE'] = "/tmp/credentials"
AWS_SHARED_CREDENTIALS_FILE = os.environ['AWS_SHARED_CREDENTIALS_FILE']

###########################################################################################
###########################################################################################
###########################################################################################
###########################################################################################
##### GLOBALS AND ENV VARS
###########################################################################################
###########################################################################################
###########################################################################################
###########################################################################################

# Load env vars (lambda will ignore this file since it will not be there):

# make  sure the env vars file is empty and exists, place in the project home dir: 

# to run app:
# bash:
# AWS_PROFILE=asima-on-msge-shared-prod ./wp-migration.py msgnetmarqudev.env

# sys.argv[1] = msgnetmarqudev.env

envVarsFile = sys.argv[1]
if os.stat(envVarsFile).st_size != 0:
    myfile = Path(envVarsFile)
    if myfile.exists ():
        with open(envVarsFile) as f:
            for line in f:
                if 'export' not in line:
                    continue
                if line.startswith('#'):
                    continue
                # Remove leading `export `
                # then, split name / value pair
                key, value = line.replace('export ', '', 1).strip().split('=', 1)
                os.environ[key] = value
    else:
        logger.info("Env variables file: {} is empty... exiting!".format(envVarsFile))
        sys.exit()
else:
    logger.info("Env variables file not found... exiting!")
    sys.exit()


MSG_WP_ENV = os.environ['MSG_WP_ENV']

# WPENGINE SFTP CREDENTIALS AND DETAILS
############################################
WPENGINE_SFTP_HOST = os.environ['WPENGINE_SFTP_HOST']

WPENGINE_SFTP_HOST_PORT = os.environ['WPENGINE_SFTP_HOST_PORT']

WPENGINE_SFTP_USERNAME = os.environ['WPENGINE_SFTP_USERNAME']

WPENGINE_SFTP_PASSWORD = os.environ['WPENGINE_SFTP_PASSWORD']

WPENGINE_INSTALL_NAME = os.environ['WPENGINE_INSTALL_NAME']

WPENGINE_CERT_URL = os.environ['WPENGINE_CERT_URL']

WPENGINE_CERT_LOCAL_PATH = "/tmp/wpengine_root_ca.pem"

WPE_DB_NAME = "wp_" + WPENGINE_INSTALL_NAME

# WPE WEBSITE BACKUP OPTIONS
############################################
MSG_WP_BACKUP_S3_BUCKET_ACCOUNT_ID_OR_ALIAS = os.environ['MSG_WP_BACKUP_S3_BUCKET_ACCOUNT_ID_OR_ALIAS']

MSG_WP_BACKUP_S3_BUCKET_REGION = os.environ['MSG_WP_BACKUP_S3_BUCKET_REGION']

MSG_WP_BACKUP_S3_BUCKET_NAME = os.environ['MSG_WP_BACKUP_S3_BUCKET_NAME']

WPE_BACKUP_CONTENT = os.environ['WPE_BACKUP_CONTENT'].upper()

WPE_BACKUP_WP_DB = os.environ['WPE_BACKUP_WP_DB'].upper()

# MSG WP BASTION
############################################
# SFTP CREDENTIALS AND DETAILS
WP_BASTION_INSTANCE_ID = os.environ['WP_BASTION_INSTANCE_ID']

WP_BASTION_AWS_ACCOUNT_ID_OR_ALIAS = os.environ['WP_BASTION_AWS_ACCOUNT_ID_OR_ALIAS']

WP_BASTION_AWS_REGION = os.environ['WP_BASTION_AWS_REGION']

WP_BASTION_SFTP_HOST_PORT = os.environ['WP_BASTION_SFTP_HOST_PORT']

PS_WP_BASTION_SFTP_PASSWORD = os.environ['PS_WP_BASTION_SFTP_PASSWORD']
# boto3.setup_default_session(profile_name=getJenkinsProfile(WP_BASTION_AWS_ACCOUNT_ID_OR_ALIAS), region_name=WP_BASTION_AWS_REGION)
# ssm = boto3.client('ssm', region_name='us-east-1')
# parameter = ssm.get_parameter(Name=PS_WP_BASTION_SFTP_PASSWORD, WithDecryption=True)
# WP_BASTION_SFTP_PASSWORD=parameter['Parameter']['Value']
WP_BASTION_SFTP_PASSWORD = getSSMSecret(WP_BASTION_AWS_ACCOUNT_ID_OR_ALIAS, WP_BASTION_AWS_REGION, PS_WP_BASTION_SFTP_PASSWORD)

WP_BASTION_SFTP_USERNAME = os.environ['WP_BASTION_SFTP_USERNAME']

# RDS AND WP DB DETAILS (Refer to terraform /1-env/.terraform.tfvars)
############################################
MSG_WP_RDS_AWS_ACCOUNT_ID_OR_ALIAS = os.environ['MSG_WP_RDS_AWS_ACCOUNT_ID_OR_ALIAS']

MSG_WP_RDS_AWS_REGION = os.environ['MSG_WP_RDS_AWS_REGION']

MSG_WP_RDS_HOST_END_POINT = os.environ['MSG_WP_RDS_HOST_END_POINT']

# MSG_WP_RDS_DB_USERNAME = os.environ['MSG_WP_RDS_DB_USERNAME']

# MSG_WP_RDS_DB_NAME = os.environ['MSG_WP_RDS_DB_NAME']

# WP_BASTION_WEBSITE_CONTENT_DIR = os.environ['WP_BASTION_WEBSITE_CONTENT_DIR']

PS_MSG_WP_RDS_DB_PASSWORD = os.environ['PS_MSG_WP_RDS_DB_PASSWORD']
# boto3.setup_default_session(profile_name=getJenkinsProfile(MSG_WP_RDS_AWS_ACCOUNT_ID_OR_ALIAS), region_name=MSG_WP_RDS_AWS_REGION)
# ssm = boto3.client('ssm', region_name='us-east-1')
# parameter = ssm.get_parameter(Name=PS_MSG_WP_RDS_DB_PASSWORD, WithDecryption=True)
# MSG_WP_RDS_DB_PASSWORD=parameter['Parameter']['Value']
MSG_WP_RDS_DB_PASSWORD = getSSMSecret(MSG_WP_RDS_AWS_ACCOUNT_ID_OR_ALIAS, MSG_WP_RDS_AWS_REGION, PS_MSG_WP_RDS_DB_PASSWORD)

# WEBSITE DOMAIN
############################################
WEBSITE_ALB_AWS_ACCOUNT_ID_OR_ALIAS = os.environ['WEBSITE_ALB_AWS_ACCOUNT_ID_OR_ALIAS']

WEBSITE_DOMAIN = os.environ['WEBSITE_DOMAIN']

WEBSITE_SUB_DOMAIN = os.environ['WEBSITE_SUB_DOMAIN']

WEBSITE_ALB_ARN = os.environ['WEBSITE_ALB_ARN']

EC2_KEYS_BUCKET_NAME="r6bi620bkogmv89g"

AWS_ACCOUNT_HOSTING_THE_DOMAIN = os.environ['AWS_ACCOUNT_HOSTING_THE_DOMAIN']

AWS_REGION_HOSTING_THE_DOMAIN = os.environ['AWS_REGION_HOSTING_THE_DOMAIN']

WEBSITE_DOMAIN_OLD = os.environ['WEBSITE_DOMAIN_OLD']

# Jenkins Details
##########################################################
JENKINS_INSTANCE_ID = os.environ['JENKINS_INSTANCE_ID']

JENKINS_AWS_ACCOUNT_ID_OR_ALIAS = os.environ['JENKINS_AWS_ACCOUNT_ID_OR_ALIAS']

JENKINS_AWS_REGION = os.environ['JENKINS_AWS_REGION']

# prepare the directory where all msg keys will be stored
##########################################################
# set path for keys
MSG_KEYS_LOCAL_FOLDER = Path('/tmp')
MSG_KEYS_LOCAL_FOLDER = MSG_KEYS_LOCAL_FOLDER.joinpath('msg-keys')
os.makedirs(str(MSG_KEYS_LOCAL_FOLDER), exist_ok=True)

# slack
#########################################################

PS_SLACK_API_KEY_AWS_ACCOUNT_ID_OR_ALIAS = os.environ['PS_SLACK_API_KEY_AWS_ACCOUNT_ID_OR_ALIAS']
PS_SLACK_API_KEY_AWS_REGION = os.environ['PS_SLACK_API_KEY_AWS_REGION']

PS_SLACK_API_KEY=os.environ['PS_SLACK_API_KEY']
# boto3.setup_default_session(profile_name=getJenkinsProfile(PS_SLACK_API_KEY_AWS_ACCOUNT_ID_OR_ALIAS), region_name=PS_SLACK_API_KEY_AWS_REGION)
# ssm = boto3.client('ssm', region_name='us-east-1')
# parameter = ssm.get_parameter(Name=PS_SLACK_API_KEY, WithDecryption=True)
# SLACK_API_KEY=parameter['Parameter']['Value']
SLACK_API_KEY = getSSMSecret(PS_SLACK_API_KEY_AWS_ACCOUNT_ID_OR_ALIAS, PS_SLACK_API_KEY_AWS_REGION, PS_SLACK_API_KEY)

# github
#########################################################

GITHUB_TOKEN_AWS_ACCOUNT_ID_OR_ALIAS = os.environ['GITHUB_TOKEN_AWS_ACCOUNT_ID_OR_ALIAS']
GITHUB_TOKEN_AWS_REGION = os.environ['GITHUB_TOKEN_AWS_REGION']
PS_GITHUB_TOKEN = os.environ['PS_GITHUB_TOKEN']
GITHUB_TOKEN = getSSMSecret(GITHUB_TOKEN_AWS_ACCOUNT_ID_OR_ALIAS, GITHUB_TOKEN_AWS_REGION, PS_GITHUB_TOKEN)
PS_GITHUB_USERNAME = os.environ['PS_GITHUB_USERNAME']

# App completion status
##########################################################
APP_COMPLETE_STATUS="INCOMPLETE"

# WPENGINE SFTP CREDENTIALS AND DETAILS
##########################################
TFVARS_AWS_PROFILE = os.environ['TFVARS_AWS_PROFILE']
TFVARS_PHASE = os.environ['TFVARS_PHASE']
TFVARS_ENV = os.environ['TFVARS_ENV']
TFVARS_PROJECT = os.environ['TFVARS_PROJECT']
TFVARS_AWS_REGION = os.environ['TFVARS_AWS_REGION']
TFVARS_AWS_AZS = os.environ['TFVARS_AWS_AZS']
TFVARS_WP_REPO_NAME = os.environ['TFVARS_WP_REPO_NAME']
TFVARS_WP_DB_NAME = os.environ['TFVARS_WP_DB_NAME']
MSG_WP_RDS_DB_NAME = TFVARS_WP_DB_NAME
TFVARS_WP_DB_USERNAME = os.environ['TFVARS_WP_DB_USERNAME']
MSG_WP_RDS_DB_USERNAME = TFVARS_WP_DB_USERNAME

PS_TFVARS_WP_DB_PASSWORD = os.environ['PS_TFVARS_WP_DB_PASSWORD']
PS_TFVARS_WP_DB_PASSWORD_AWS_ACCOUNT = os.environ['PS_TFVARS_WP_DB_PASSWORD_AWS_ACCOUNT']
PS_TFVARS_WP_DB_PASSWORD_AWS_REGION = os.environ['PS_TFVARS_WP_DB_PASSWORD_AWS_REGION']
TFVARS_WP_DB_PASSWORD = getSSMSecret(PS_TFVARS_WP_DB_PASSWORD_AWS_ACCOUNT, PS_TFVARS_WP_DB_PASSWORD_AWS_REGION, PS_TFVARS_WP_DB_PASSWORD)

TFVARS_WP_DB_CHARSET = os.environ['TFVARS_WP_DB_CHARSET']
TFVARS_WP_DB_COLLATE = os.environ['TFVARS_WP_DB_COLLATE']
TFVARS_WP_TABLE_PREFIX = os.environ['TFVARS_WP_TABLE_PREFIX']
TFVARS_WP_DOMAIN = WEBSITE_SUB_DOMAIN + '.' + WEBSITE_DOMAIN
TFVARS_LAMBDA_ENV = os.environ['TFVARS_LAMBDA_ENV']
TFVARS_CONTAINER_TAG = os.environ['TFVARS_CONTAINER_TAG']
TFVARS_INSTALL_PRODUCTION = os.environ['TFVARS_INSTALL_PRODUCTION']
TFVARS_STATE_FILE_BUCKET = os.environ['TFVARS_STATE_FILE_BUCKET']
TFVARS_STATE_FILE_ACCOUNT = os.environ['TFVARS_STATE_FILE_ACCOUNT']
TFVARS_STATE_FILE_REGION = os.environ['TFVARS_STATE_FILE_REGION']

# from: host_path = "/mnt/${var.project}-${var.env}/wp-content"
# on: https://github.com/MSGTech/msg-wordpress-terraform/blob/master/1-env/ecs.tf

WP_BASTION_WEBSITE_CONTENT_DIR = "/mnt/" + TFVARS_PROJECT + '-' + TFVARS_ENV
###########################################################################################
###########################################################################################
###########################################################################################
###########################################################################################
# # # # # Logging
###########################################################################################
###########################################################################################
###########################################################################################
###########################################################################################

######### Create the logger:
logger = logging.getLogger('WP MIGRATION')

######### Set log level:
logger.setLevel(logging.INFO)

######### define formatter:
format_str = '%(asctime)s,ms(%(msecs)03d)  | %(levelname)-8s | %(name)s | %(message)s'
date_format = '%a %d-%b-%Y %H:%M:%S %z %Z'
formatter = logging.Formatter(format_str, date_format)

######### define file handler for logging now that the install name is set:
logFileTimeStamp = time.strftime("%a-%d-%b-%Y-%H%M%S")
logFileName = 'wp-migration-' + logFileTimeStamp + '-' + WPENGINE_INSTALL_NAME + '.log'
logFileName = 'wp-migration-' + logFileTimeStamp + '.log'
logFilePath = '/tmp/' + logFileName

file_handler = logging.FileHandler(logFilePath) # should be disabled for lambda
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

######### define stream handler to log to console
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

######### define cloudwatch




###########################################################################################
###########################################################################################
###########################################################################################
###########################################################################################
##### MAIN
###########################################################################################
###########################################################################################
###########################################################################################
###########################################################################################

def main(event=None, context=None):

    # replaced with the local file: wpe-install-migration-progress.json
    # # get progress file:
    # boto3.setup_default_session(profile_name=getJenkinsProfile("msge-shared-prod"), region_name="us-east-1")
    # download_s3_file("msg-devops-scripts", "wpe-install-migration-progress.json", "wpe-install-migration-progress.json")

# def main():
    APP_COMPLETE_STATUS = "INCOMPLETE"

    # start timer
    start_time = datetime.now()
    eastern = timezone('US/Eastern')
    wpe_localized_timestamp = eastern.localize(start_time)
    WPE_BACKUP_TIMESTAMP = wpe_localized_timestamp.strftime("%a-%d-%b-%Y(%H-%M-%S-%Z%z)")

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # BUILDING THE ENV USING TERRAFORM
    #######################################################################
    #######################################################################
    #######################################################################
    ########################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("BUILDING THE WEBSITE ENV USING TERRAFORM")
    print("#######################################################################")
    print("#######################################################################")

    migration_status_dict = get_migration_status(WPENGINE_INSTALL_NAME)
    websiteTerraformEnvStatus = migration_status_dict["websiteTerraformStatus"]
    if websiteTerraformEnvStatus == "COMPLETE":

        logger.info(
            "Terraform env for the install: {} has already been applied, terraform status is: {}.".format(
                migration_status_dict["wpeInstallName"],
                migration_status_dict["websiteTerraformStatus"]
            )
        )

        userReply = get_user_yes_no_reply("Build terraform env again?")
    
        if userReply == True:

            buildWebsiteTerraformEnv(
                WPENGINE_INSTALL_NAME,
                PS_GITHUB_USERNAME,
                GITHUB_TOKEN,
                TFVARS_AWS_PROFILE,
                TFVARS_PHASE,
                TFVARS_ENV,
                TFVARS_PROJECT,
                TFVARS_AWS_REGION,
                TFVARS_AWS_AZS,
                TFVARS_WP_REPO_NAME,
                TFVARS_WP_DB_NAME,
                TFVARS_WP_DB_USERNAME,
                TFVARS_WP_DB_PASSWORD,
                TFVARS_WP_DB_CHARSET,
                TFVARS_WP_DB_COLLATE,
                TFVARS_WP_TABLE_PREFIX,
                TFVARS_WP_DOMAIN,
                TFVARS_CONTAINER_TAG,
                TFVARS_LAMBDA_ENV,
                TFVARS_INSTALL_PRODUCTION,
                TFVARS_STATE_FILE_BUCKET,
                TFVARS_STATE_FILE_ACCOUNT,
                TFVARS_STATE_FILE_REGION
            )

    else:

        buildWebsiteTerraformEnv(
            WPENGINE_INSTALL_NAME,
            PS_GITHUB_USERNAME,
            GITHUB_TOKEN,
            TFVARS_AWS_PROFILE,
            TFVARS_PHASE,
            TFVARS_ENV,
            TFVARS_PROJECT,
            TFVARS_AWS_REGION,
            TFVARS_AWS_AZS,
            TFVARS_WP_REPO_NAME,
            TFVARS_WP_DB_NAME,
            TFVARS_WP_DB_USERNAME,
            TFVARS_WP_DB_PASSWORD,
            TFVARS_WP_DB_CHARSET,
            TFVARS_WP_DB_COLLATE,
            TFVARS_WP_TABLE_PREFIX,
            TFVARS_WP_DOMAIN,
            TFVARS_CONTAINER_TAG,
            TFVARS_LAMBDA_ENV,
            TFVARS_INSTALL_PRODUCTION,
            TFVARS_STATE_FILE_BUCKET,
            TFVARS_STATE_FILE_ACCOUNT,
            TFVARS_STATE_FILE_REGION
        )

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # PREPARE CONNECTIONS TO JENKINS AND BASTION
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("PREPARE CONNECTIONS TO JENKINS AND BASTION")
    print("#######################################################################")
    print("#######################################################################")

    # Get wpengine cert from taking mysql backup from wpengine
    #######################################################################

    wpEngineResponse = requests.get("https://storage.googleapis.com/wpengine-public-assets/certificates/wpengine_root_ca.pem")
    with open(WPENGINE_CERT_LOCAL_PATH, 'wb') as myfile:
        myfile.write(wpEngineResponse.content)

    # get jenkins details:
    #############################################
    jenkinsInstanceID = JENKINS_INSTANCE_ID
    jenkinsAWSProfile = getJenkinsProfile(JENKINS_AWS_ACCOUNT_ID_OR_ALIAS)
    jenkinsECsRegion = JENKINS_AWS_REGION
    jenkinsDetails = getEC2DetailsById(jenkinsInstanceID, jenkinsAWSProfile,jenkinsECsRegion)
    jenkinsKeyName = jenkinsDetails["KeyName"]

    # get wp bastion details
    #############################################
    wpBastionInstanceId = WP_BASTION_INSTANCE_ID
    wpBastionAWSProfile = getJenkinsProfile(WP_BASTION_AWS_ACCOUNT_ID_OR_ALIAS)
    wpBastionEcsRegion = WP_BASTION_AWS_REGION
    wpBastionDetails = getEC2DetailsById(wpBastionInstanceId, wpBastionAWSProfile,wpBastionEcsRegion)
    wpBastionKeyName = wpBastionDetails["KeyName"]
    wpBastionEC2User = wpBastionDetails["InstanceUser"]
    wpBastionPublicIp = wpBastionDetails["InstancePublicIp"]
    wpBastionName = wpBastionDetails["InstanceNameTag"]

    # connect to jenkins
    #################################
    jenkinsSSHClient = openSSHConToEC2(
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion
    )

    # keep the SSH connection open:
    # create args tuple for the runCommandOnEc2 function
    ##########################################
    timerArgs = (
        "Keep SSH CLient Open.", 
        "echo", 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )
    appTimer = threading.Timer(600.0, runCommandOnEc2, args=timerArgs)
    appTimer.start()

    jenkinsCommand = "aws s3 cp s3://{}/{} ~/.ssh/{}".format(
        EC2_KEYS_BUCKET_NAME,
        wpBastionKeyName, 
        wpBastionKeyName
    )
    commandResult = runCommandOnEc2(
        "copy bastion key on jenkins", 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    jenkinsCommand = "sudo chmod 400 ~/.ssh/{}".format(wpBastionKeyName)
    commandResult = runCommandOnEc2(
        "restrict key permissions.", 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # make sure this sftp user is created on wp bastion nonprod
    #######################################################################

    jenkinsCommand = "ssh -i ~/.ssh/{} {}@{} 'getent passwd {}'".format(
        wpBastionKeyName, 
        wpBastionEC2User, 
        wpBastionPublicIp, 
        WP_BASTION_SFTP_USERNAME
    )

    commandResult = runCommandOnEc2(
        "check if user msgsftpuser is on wp bastion non prod", 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )
    logger.info(commandResult["std_out"])

    if WP_BASTION_SFTP_USERNAME in commandResult['std_out']:
        logger.info("sftp user: {} already exists on {} - {}".format(WP_BASTION_SFTP_USERNAME, wpBastionInstanceId, wpBastionName))
    else:
        logger.info("sftp user: {} does not exist on {} - {}, please create one... exiting".format(WP_BASTION_SFTP_USERNAME, 
        wpBastionInstanceId, wpBastionName))
        sys.exit()

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # BACKING UP WPE DB TO S3
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("BACKING UP WPE WP DB TO S3")
    print("#######################################################################")
    print("#######################################################################")

    migration_status_dict = get_migration_status(WPENGINE_INSTALL_NAME)
    backupWPEWPDBToS3Status = migration_status_dict["backupWPEWPDBToS3Status"]
    if backupWPEWPDBToS3Status == "COMPLETE":

        logger.info(
            "The wp db backup for the install: {} has already been done, status: {}".format(
                migration_status_dict["wpeInstallName"],
                migration_status_dict["backupWPEWPDBToS3Status"]
            )
        )

        userReply = get_user_yes_no_reply("Backup WPE WP DB Again?")
    
        if userReply == True:

            backupWPEWPDBToS3(
                WPENGINE_INSTALL_NAME,
                jenkinsInstanceID, 
                jenkinsAWSProfile, 
                jenkinsECsRegion, 
                jenkinsSSHClient,
                WPENGINE_CERT_URL,
                WPENGINE_CERT_LOCAL_PATH,
                MSG_WP_RDS_HOST_END_POINT,
                MSG_WP_RDS_DB_USERNAME,
                MSG_WP_RDS_DB_PASSWORD,
                WPE_DB_NAME
            )

    else:

        backupWPEWPDBToS3(
            WPENGINE_INSTALL_NAME,
            jenkinsInstanceID, 
            jenkinsAWSProfile, 
            jenkinsECsRegion, 
            jenkinsSSHClient,
            WPENGINE_CERT_URL,
            WPENGINE_CERT_LOCAL_PATH,
            MSG_WP_RDS_HOST_END_POINT,
            MSG_WP_RDS_DB_USERNAME,
            MSG_WP_RDS_DB_PASSWORD,
            WPE_DB_NAME
        )

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # WP DB RESTORE OPERATIONS:
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("WP DB RESTORE OPERATIONS - INCLUDES CREATING THE DB ON THE RDS")
    print("#######################################################################")
    print("#######################################################################")

    migration_status_dict = get_migration_status(WPENGINE_INSTALL_NAME)
    wpWebsiteDBRestorationStatus = migration_status_dict["dbRestoreStatus"]
    if wpWebsiteDBRestorationStatus == "COMPLETE":

        logger.info(
            "DB restoration of the wpe install: {} to the AWS RDS has already been done, DB restoration status is: {}.".format(
                migration_status_dict["wpeInstallName"], 
                migration_status_dict["dbRestoreStatus"]
            )
        )

        userReply = get_user_yes_no_reply("Restore Again?")
    
        if userReply == True:

            restoreWPDBOnAWSEnv(
                WPENGINE_INSTALL_NAME,
                wpBastionKeyName,
                wpBastionEC2User,
                wpBastionPublicIp,
                WP_BASTION_WEBSITE_CONTENT_DIR,
                jenkinsInstanceID, 
                jenkinsAWSProfile, 
                jenkinsECsRegion, 
                jenkinsSSHClient,
                WPENGINE_CERT_URL,
                WPENGINE_CERT_LOCAL_PATH,
                MSG_WP_RDS_HOST_END_POINT,
                MSG_WP_RDS_DB_USERNAME,
                MSG_WP_RDS_DB_PASSWORD
            )

    else:

        restoreWPDBOnAWSEnv(
            WPENGINE_INSTALL_NAME,
            wpBastionKeyName,
            wpBastionEC2User,
            wpBastionPublicIp,
            WP_BASTION_WEBSITE_CONTENT_DIR,
            jenkinsInstanceID, 
            jenkinsAWSProfile, 
            jenkinsECsRegion, 
            jenkinsSSHClient,
            WPENGINE_CERT_URL,
            WPENGINE_CERT_LOCAL_PATH,
            MSG_WP_RDS_HOST_END_POINT,
            MSG_WP_RDS_DB_USERNAME,
            MSG_WP_RDS_DB_PASSWORD
        )

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # UPDATE URL IN WP DB
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("UPDATE URL IN AWS WP DB")
    print("#######################################################################")
    print("#######################################################################")

    migration_status_dict = get_migration_status(WPENGINE_INSTALL_NAME)
    updateWebsiteURLInWPDBStatus = migration_status_dict["updateWebsiteURLinWPDBStatus"]
    if updateWebsiteURLInWPDBStatus == "COMPLETE":

        logger.info(
            "Website URL of the wpe install: {} has already been updated from: {} to: {}, status is: {}".format(
                migration_status_dict["wpeInstallName"],
                WEBSITE_DOMAIN_OLD,
                TFVARS_WP_DOMAIN,
                migration_status_dict["dbRestoreStatus"]
            )
        )

        userReply = get_user_yes_no_reply("Update Website URL Again?")
    
        if userReply == True:

            updateWebsiteURLInWPDB(
                WPENGINE_INSTALL_NAME,
                TFVARS_WP_DOMAIN,
                WEBSITE_DOMAIN_OLD,
                jenkinsInstanceID, 
                jenkinsAWSProfile, 
                jenkinsECsRegion, 
                jenkinsSSHClient,
                wpBastionKeyName,
                wpBastionEC2User, 
                wpBastionPublicIp
            )

    else:

        updateWebsiteURLInWPDB(
            WPENGINE_INSTALL_NAME,
            TFVARS_WP_DOMAIN,
            WEBSITE_DOMAIN_OLD,
            jenkinsInstanceID, 
            jenkinsAWSProfile, 
            jenkinsECsRegion, 
            jenkinsSSHClient,
            wpBastionKeyName,
            wpBastionEC2User, 
            wpBastionPublicIp
        )

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # backup content to s3
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################

    # # check user asim.abdelgadir@msg.com in wp db:
    # buildWpCheckUserExistScriptFile()

    # # download the script file to jenkins:
    # jenkinsCommand = "aws s3 cp s3://{}/{} /tmp/{}".format("msg-devops-scripts" ,"check_wp_user_exist.sh", "check_wp_user_exist.sh")
    # commandResult = runCommandOnEc2("copy script file: check_wp_user_exist.sh to jenkins.", jenkinsCommand, 
    # jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)

    # # send the script file to the bastion host:
    # jenkinsCommand = "scp -i ~/.ssh/{} /tmp/check_wp_user_exist.sh {}@{}:/tmp/".format(wpBastionKeyName, 
    # wpBastionEC2User, wpBastionPublicIp)
    # commandResult = runCommandOnEc2("copy script file: check_wp_user_exist.sh to bastion host.", jenkinsCommand,
    # jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)

    # # modify script permissions:
    # jenkinsCommand = "ssh -i ~/.ssh/{} {}@{} 'sudo chmod a+x /tmp/check_wp_user_exist.sh'".format(wpBastionKeyName, 
    # wpBastionEC2User, wpBastionPublicIp)
    # commandResult = runCommandOnEc2("set executable permissions on the script file: /tmp/check_wp_user_exist.sh", jenkinsCommand, 
    # jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)
    # logger.info(commandResult["std_out"])

    # # run script on remote:
    # jenkinsCommand = "ssh -i ~/.ssh/{} {}@{} 'bash /tmp/check_wp_user_exist.sh'".format(wpBastionKeyName, 
    # wpBastionEC2User, wpBastionPublicIp)
    # commandResult = runCommandOnEc2("run script file: /tmp/check_wp_user_exist.sh", jenkinsCommand, 
    # jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)
    # logger.info(commandResult["std_out"])

    # if 'asim.abdelgadir@msg.com' in commandResult["std_out"]:
    #     logger.info("The user: asim.abdelgadir@msg.com already exists in the wp db: {}".format(MSG_WP_RDS_DB_NAME))
    #     # reset password:

    #     # create the script file:
    #     buildWpUpdateUserPasswordScriptFile()

    #     # download the script file to jenkins:
    #     jenkinsCommand = "aws s3 cp s3://{}/{} /tmp/{}".format("msg-devops-scripts" ,"update_wp_user_pass.sh", "update_wp_user_pass.sh")
    #     commandResult = runCommandOnEc2("copy script file: update_wp_user_pass.sh to jenkins.", jenkinsCommand, 
    #     jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)

    #     # send the script file to the bastion host:
    #     jenkinsCommand = "scp -i ~/.ssh/{} /tmp/update_wp_user_pass.sh {}@{}:/tmp/".format(wpBastionKeyName, 
    #     wpBastionEC2User, wpBastionPublicIp)
    #     commandResult = runCommandOnEc2("copy script file: update_wp_user_pass.sh to bastion host.", jenkinsCommand,
    #     jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)

    #     # modify script permissions:
    #     jenkinsCommand = "ssh -i ~/.ssh/{} {}@{} 'sudo chmod a+x /tmp/update_wp_user_pass.sh'".format(wpBastionKeyName, 
    #     wpBastionEC2User, wpBastionPublicIp)
    #     commandResult = runCommandOnEc2("set executable permissions on the script file: /tmp/update_wp_user_pass.sh", jenkinsCommand, 
    #     jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)
    #     logger.info(commandResult["std_out"])

    #     # run script on remote:
    #     jenkinsCommand = "ssh -i ~/.ssh/{} {}@{} 'bash /tmp/update_wp_user_pass.sh'".format(wpBastionKeyName, 
    #     wpBastionEC2User, wpBastionPublicIp)
    #     commandResult = runCommandOnEc2("run script file: /tmp/update_wp_user_pass.sh", jenkinsCommand, 
    #     jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)
    #     logger.info(commandResult["std_out"])

    # else:
    #     # create the user

    #     # create the script file:
    #     buildWpCreateUserScriptFile()

    #     # download the script file to jenkins:
    #     jenkinsCommand = "aws s3 cp s3://{}/{} /tmp/{}".format("msg-devops-scripts" ,"create_wp_user.sh", "create_wp_user.sh")
    #     commandResult = runCommandOnEc2("copy script file: create_wp_user.sh to jenkins.", jenkinsCommand, 
    #     jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)

    #     # send the script file to the bastion host:
    #     jenkinsCommand = "scp -i ~/.ssh/{} /tmp/create_wp_user.sh {}@{}:/tmp/".format(wpBastionKeyName, 
    #     wpBastionEC2User, wpBastionPublicIp)
    #     commandResult = runCommandOnEc2("copy script file: create_wp_user.sh to bastion host.", jenkinsCommand,
    #     jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)

    #     # modify script permissions:
    #     jenkinsCommand = "ssh -i ~/.ssh/{} {}@{} 'sudo chmod a+x /tmp/create_wp_user.sh'".format(wpBastionKeyName, 
    #     wpBastionEC2User, wpBastionPublicIp)
    #     commandResult = runCommandOnEc2("set executable permissions on the script file: /tmp/create_wp_user.sh", jenkinsCommand, 
    #     jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)
    #     logger.info(commandResult["std_out"])

    #     # run script on remote:
    #     jenkinsCommand = "ssh -i ~/.ssh/{} {}@{} 'bash /tmp/create_wp_user.sh'".format(wpBastionKeyName, 
    #     wpBastionEC2User, wpBastionPublicIp, WP_BASTION_SFTP_USERNAME)
    #     commandResult = runCommandOnEc2("run script file: /tmp/update_wp_user_pass.sh", jenkinsCommand, 
    #     jenkinsInstanceID, jenkinsAWSProfile, jenkinsECsRegion, jenkinsSSHClient)
    #     logger.info(commandResult["std_out"])

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # point domain to alb:
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("POINT THE DOMAIN TO THE ALB")
    print("#######################################################################")
    print("#######################################################################")

    migration_status_dict = get_migration_status(WPENGINE_INSTALL_NAME)
    setWebsiteDomainInRoute53Status = migration_status_dict["setWebsiteDomainInRoute53Status"]
    if setWebsiteDomainInRoute53Status == "COMPLETE":

        logger.info(
            "website domain already set, the domain {} is pointing to the alb {}... status: {}".format(
                TFVARS_WP_DOMAIN,
                WEBSITE_ALB_ARN, 
                migration_status_dict["setWebsiteDomainInRoute53Status"]
            )
        )

        userReply = get_user_yes_no_reply("Do you want to point the domain to the alb again?")
    
        if userReply == True:

            pointDomainToAlb(
                WEBSITE_ALB_ARN,
                AWS_ACCOUNT_HOSTING_THE_DOMAIN,
                AWS_REGION_HOSTING_THE_DOMAIN,
                WEBSITE_DOMAIN,
                WEBSITE_SUB_DOMAIN,
                WPENGINE_INSTALL_NAME
            )

    else:

        pointDomainToAlb(
            WEBSITE_ALB_ARN,
            AWS_ACCOUNT_HOSTING_THE_DOMAIN,
            AWS_REGION_HOSTING_THE_DOMAIN,
            WEBSITE_DOMAIN,
            WEBSITE_SUB_DOMAIN,
            WPENGINE_INSTALL_NAME
        )

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # Set file ownership on efs content folder before copying content
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("SET FILE OWNERSHIP ON CONTENT DIR ON /mnt BEFORE COPYING CONTENT")
    print("#######################################################################")
    print("#######################################################################")

    migration_status_dict = get_migration_status(WPENGINE_INSTALL_NAME)
    contentFileOwnershipStatus = migration_status_dict["contentFileOwnershipStatus"]
    if contentFileOwnershipStatus == "COMPLETE":

        logger.info(
            "Content dir {} file ownership already modified to wordpress:sftpusers ... status: {}".format(
                WP_BASTION_WEBSITE_CONTENT_DIR,
                migration_status_dict["contentFileOwnershipStatus"]
            )
        )    

        userReply = get_user_yes_no_reply("Do you want to set file ownership again?")
    
        if userReply == True:

            setFileOwnershipOnContentDir(
                WPENGINE_INSTALL_NAME,
                wpBastionKeyName,
                wpBastionEC2User,
                wpBastionPublicIp,
                WP_BASTION_WEBSITE_CONTENT_DIR,
                jenkinsInstanceID,
                jenkinsAWSProfile,
                jenkinsECsRegion,
                jenkinsSSHClient
            )

    else:

        setFileOwnershipOnContentDir(
            WPENGINE_INSTALL_NAME,
            wpBastionKeyName,
            wpBastionEC2User,
            wpBastionPublicIp,
            WP_BASTION_WEBSITE_CONTENT_DIR,
            jenkinsInstanceID,
            jenkinsAWSProfile,
            jenkinsECsRegion,
            jenkinsSSHClient
        )

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # Set file permissions on efs content folder before copying content
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("SET FILE PERMISSION TO WRIATABLE ON CONTENT DIR /mnt")
    print("#######################################################################")
    print("#######################################################################")

    migration_status_dict = get_migration_status(WPENGINE_INSTALL_NAME)
    contentFilePermissionStatus = migration_status_dict["contentFilePermissionStatus"]
    if contentFilePermissionStatus == "COMPLETE":

        logger.info(
            "Content dir {} file ownership already modified to writable... status: {}".format(
                WP_BASTION_WEBSITE_CONTENT_DIR,
                migration_status_dict["contentFilePermissionStatus"]
            )
        )

        userReply = get_user_yes_no_reply("Do you want to set file permissions again?")
    
        if userReply == True:

            setFilePernissionsOnContentDir(
                WPENGINE_INSTALL_NAME,
                wpBastionKeyName,
                wpBastionEC2User,
                wpBastionPublicIp,
                WP_BASTION_WEBSITE_CONTENT_DIR,
                jenkinsInstanceID,
                jenkinsAWSProfile,
                jenkinsECsRegion,
                jenkinsSSHClient
            )

    else:

        setFilePernissionsOnContentDir(
            WPENGINE_INSTALL_NAME,
            wpBastionKeyName,
            wpBastionEC2User,
            wpBastionPublicIp,
            WP_BASTION_WEBSITE_CONTENT_DIR,
            jenkinsInstanceID,
            jenkinsAWSProfile,
            jenkinsECsRegion,
            jenkinsSSHClient
        )

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # COPY WPE CONTENT TO EFS
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("COPY CONTENT FROM WPE TO CONTENT DIR /mnt")
    print("#######################################################################")
    print("#######################################################################")

    # 10MB Chuncks
    # CHUNK_SIZE = 6291456
    CHUNK_SIZE = 10000000

    # SFTP Copy Source Directory:
    # trailing slash is optional for all subdirectories: remoteSftpDir = "/testasim/testasim2/" or remoteSftpDir = "/testasim/testasim2" are both ok.
    # If you want to transfer the entire wbsite then: remoteSftpDir = "/"
    remoteSftpDir = "/"

    # initiate s3 key var and ftp file path var
    S3FileKey = ""
    sftpFilePath = ""

    # CONNECT TO WPENGINE SFTP HOST:
    sftpConnection = openSFTPConToWpe(WPENGINE_SFTP_HOST, int(WPENGINE_SFTP_HOST_PORT), WPENGINE_SFTP_USERNAME, WPENGINE_SFTP_PASSWORD)

    # CONNECT TO WP BASTION SFTP HOST:
    sftpWpBastionConnection = openSFTPConToEc2(
        wpBastionInstanceId,
        wpBastionAWSProfile,
        wpBastionEcsRegion,
        wpBastionPublicIp, 
        int(WP_BASTION_SFTP_HOST_PORT), 
        WP_BASTION_SFTP_USERNAME, 
        WP_BASTION_SFTP_PASSWORD
    )

    # send files one at a time here:
    
    if sftpWpBastionConnection == 'conn_error':
        logger.error('Failed to connect FTP Server: {} ... EXITING!'.format(wpBastionPublicIp))
        sys.exit()

    if sftpWpBastionConnection == 'auth_error':
        logger.error('Incorrect username or password for: {} ... EXITING!'.format(wpBastionPublicIp))
        sys.exit()

    if sftpConnection == 'conn_error':
        logger.error('Failed to connect FTP Server: {} ... EXITING!'.format(WPENGINE_SFTP_HOST))
        sys.exit()

    if sftpConnection == 'auth_error':
        logger.error('Incorrect username or password for: {} ... EXITING!'.format(WPENGINE_SFTP_HOST))
        sys.exit()

    totalInstallSize = 0
    fileSize = 0

    migration_status_dict = get_migration_status(WPENGINE_INSTALL_NAME)
    if migration_status_dict["websiteContentCopyStatus"] != "COMPLETE":

        # create the content dir
        jenkinsCommand = "ssh -i ~/.ssh/{} {}@{} 'mkdir -p {}'".format(
            wpBastionKeyName, 
            wpBastionEC2User, 
            wpBastionPublicIp, 
            WP_BASTION_WEBSITE_CONTENT_DIR
        )
        commandResult = runCommandOnEc2(
            "create the content directory, this might take a very long time, please wait.", 
            jenkinsCommand, 
            jenkinsInstanceID, 
            jenkinsAWSProfile, 
            jenkinsECsRegion, 
            jenkinsSSHClient
        )

        # copy content
        logger.info(commandResult["std_out"])
        logger.info("Copying content from wpe to wordpress bastion, this might take a very long time, please wait.")

        for path,files  in sftp_walk(sftpConnection, remoteSftpDir):
            for file in files:
                #get full path of sftp file
                sftpFilePath = os.path.join(os.path.join(path,file))
                # get size:
                sftpFileStats = sftpConnection.stat(sftpFilePath)
                fileSize = sftpFileStats.st_size
                # increment size
                totalInstallSize = totalInstallSize + fileSize
                # Get dir of sftp file
                # sftp_file_dir_name = os.path.dirname(sftpFilePath)
                # get name of sftp file
                # ftp_file_name = os.path.basename(sftpFilePath)
                # create S3 Key(S3 File path, this is the full path of the s3 object minus the s3 bucket name)
                # S3FileKey = WPENGINE_INSTALL_NAME + ftp_file_dir_name + ftp_file_name
                S3FileKey = WPENGINE_INSTALL_NAME + "/WPContent/" + WPE_BACKUP_TIMESTAMP + "/" + sftpFilePath
                # format file size to humnan readable
                readableFileSize = humanReadableDataSize(fileSize)

                ##################
                # copy to s3 - tkes 2h:50m - not required since backups are already taken on wpe, this should be from the /mnt to S3
                ##################
                # display task

                # if WPE_BACKUP_CONTENT == "YES":
                #     logger.info("Copying: {} TO S3 KEY => {} Size: {}".format(sftpFilePath, S3FileKey, readableFileSize))
                #     transfer_file_from_ftp_to_s3(sftpConnection, MSG_WP_BACKUP_S3_BUCKET_NAME, sftpFilePath, S3FileKey, CHUNK_SIZE)
            
                ##################
                # copy to /mnt takes 2h:20m
                ##################
                sftpFilePathDest = WP_BASTION_WEBSITE_CONTENT_DIR + sftpFilePath
                logger.info("Copying: {} TO MNT ON PREPROD BASTION => {} Size: {}".format(sftpFilePath, sftpFilePathDest, readableFileSize))

                transfer_file_from_sftp_to_sftp(sftpConnection, sftpWpBastionConnection, sftpFilePath, sftpFilePathDest, CHUNK_SIZE)

        # show total size
        logger.info("Website Content Copy complete! ====================")
        logger.info("Total Size: {}".format(humanReadableDataSize(totalInstallSize)))

        #update migration status:
        update_migration_status(
            WPENGINE_INSTALL_NAME,
            "websiteContentCopyStatus",
            "COMPLETE"
        )

    else:
        logger.info(
            "Content of the wpe install: {} has already been copied to the wordpress bastion host, content copy status is: {}.".format(
                migration_status_dict["wpeInstallName"], 
                migration_status_dict["websiteContentCopyStatus"]
            )
        )

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # Get AWS WP VERSION FROM BASTION: MAKE SURE IT MATCHES THE VERSION SUPPLIED BY THE USER IN TERRAFORM
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("Get AWS WP VERSION FROM BASTION")
    print("#######################################################################")
    print("#######################################################################")

    # build script file:
    getAWSWPVersionScripFileName = "get_aws_wp_version.sh"
    
    buildAWSWpVersionScriptFile(
        getAWSWPVersionScripFileName,
        MSG_WP_RDS_HOST_END_POINT,
        TFVARS_WP_DB_NAME,
        TFVARS_WP_DB_USERNAME,
        TFVARS_WP_DB_PASSWORD
    )

    # move the script file from here to S3:
    boto3.setup_default_session(profile_name='jenkins-on-msge-shared-prod', region_name="us-east-1")
    upload_file_to_s3("msg-devops-scripts", getAWSWPVersionScripFileName, "/tmp/{}".format(getAWSWPVersionScripFileName))

    # download file from s3 to jenkins
    jenkinsCommand = "aws s3 cp s3://msg-devops-scripts/{} /tmp/ --profile jenkins-on-msge-shared-prod".format(getAWSWPVersionScripFileName)

    commandResult = runCommandOnEc2(
        "Downloding get_aws_wp_version.sh file to jenkins", 
        jenkinsCommand,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # move script file from jenkins to bastion:
    jenkinsCommand = "scp -i ~/.ssh/{} /tmp/{} {}@{}:/tmp/".format(
        wpBastionKeyName,
        getAWSWPVersionScripFileName,
        wpBastionEC2User, 
        wpBastionPublicIp
    )
    commandResult = runCommandOnEc2(
        "move {} file from jenkins to wp bastion non prod.".format(getAWSWPVersionScripFileName), 
        jenkinsCommand,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # set file permissions:
    jenkinsCommand = '''ssh -i ~/.ssh/{} {}@{} "sudo chmod a+x /tmp/{}"'''.format(
        wpBastionKeyName, 
        wpBastionEC2User, 
        wpBastionPublicIp,
        getAWSWPVersionScripFileName
    )
    commandResult = runCommandOnEc2(
        "Making the script file /tmp/{} executable.".format(getAWSWPVersionScripFileName), 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # run script:
    jenkinsCommand = '''ssh -i ~/.ssh/{} {}@{} "bash /tmp/{}"'''.format(
        wpBastionKeyName, 
        wpBastionEC2User, 
        wpBastionPublicIp,
        getAWSWPVersionScripFileName
    )
    commandResult = runCommandOnEc2(
        "running the script file /tmp/{}".format(getAWSWPVersionScripFileName), 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    awsWPVersion = commandResult["std_out"]

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # Get WPE WP VERSION FROM WPE: MAKE SURE IT MATCHES THE VERSION SUPPLIED BY THE USER IN TERRAFORM
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("Get WPE WP VERSION FROM WPE Install")
    print("#######################################################################")
    print("#######################################################################")

    # build script file:
    getWPEWPVersionScripFileName = "get_wpe_wp_version.sh"

    buildWPEWpVersionScriptFile(
        getWPEWPVersionScripFileName,
        WPENGINE_SFTP_HOST,
        WPENGINE_SFTP_HOST_PORT,
        WPENGINE_SFTP_USERNAME,
        WPENGINE_SFTP_PASSWORD,
        WPENGINE_CERT_URL, 
        WPENGINE_CERT_LOCAL_PATH,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # move the script file from here to S3:
    boto3.setup_default_session(profile_name='jenkins-on-msge-shared-prod', region_name="us-east-1")
    upload_file_to_s3("msg-devops-scripts", getWPEWPVersionScripFileName, "/tmp/{}".format(getWPEWPVersionScripFileName))

    # download file from s3 to jenkins
    jenkinsCommand = "aws s3 cp s3://msg-devops-scripts/{} /tmp/ --profile jenkins-on-msge-shared-prod".format(getWPEWPVersionScripFileName)

    commandResult = runCommandOnEc2(
        "Downloding {} file to jenkins".format(getWPEWPVersionScripFileName), 
        jenkinsCommand,
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # set file permissions on jenkins:
    jenkinsCommand = '''sudo chmod a+x /tmp/{}"'''.format(
        getWPEWPVersionScripFileName
    )
    commandResult = runCommandOnEc2(
        "Making the script file /tmp/{} executable.".format(getWPEWPVersionScripFileName), 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    # run script:
    jenkinsCommand = '''bash /tmp/{}'''.format(
        getWPEWPVersionScripFileName
    )
    commandResult = runCommandOnEc2(
        "running the script file /tmp/{}".format(getWPEWPVersionScripFileName), 
        jenkinsCommand, 
        jenkinsInstanceID, 
        jenkinsAWSProfile, 
        jenkinsECsRegion, 
        jenkinsSSHClient
    )

    wpeWPVersion = commandResult["std_out"]

    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # COMPARE WP Version
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("Compare WP Versions")
    print("#######################################################################")
    print("#######################################################################")

    if awsWPVersion != wpeWPVersion:
        logger.info("AWS WP Version does not match WPE WP Version!")
        logger.info("AWS WP Version = {}".format(awsWPVersion))
        logger.info("WPE WP Version = {}".format(wpeWPVersion))
    else:
        logger.info("AWS WP Version and WPE WP Version match!")
        logger.info("AWS WP Version = {}".format(awsWPVersion))
        logger.info("WPE WP Version = {}".format(wpeWPVersion))


    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    # GOODBYE
    #######################################################################
    #######################################################################
    #######################################################################
    #######################################################################
    print("#######################################################################")
    print("#######################################################################")
    print("DONE...")
    print("#######################################################################")
    print("#######################################################################")

    jenkinsSSHClient.close()
    sftpConnection.close()
    sftpWpBastionConnection.close()

    # stop timer
    time_elapsed = datetime.now() - start_time
    logger.info('This migration took {}'.format(formatTimeDelta(time_elapsed)))

    APP_COMPLETE_STATUS="COMPLETE"
    appTimer.cancel()
    duration_message = 'This migration took {}'.format(formatTimeDelta(time_elapsed))
    goodbye(APP_COMPLETE_STATUS, duration_message)

main()
