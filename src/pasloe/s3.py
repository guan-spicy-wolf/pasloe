import aioboto3
from typing import Dict, Any
import uuid

from .config import get_settings

async def generate_presigned_url(filename: str, content_type: str) -> Dict[str, Any]:
    settings = get_settings()
    
    if not settings.s3_endpoint or not settings.s3_bucket:
        raise ValueError("S3 is not configured")

    import os
    dir_name, file_name = os.path.split(filename)
    if dir_name:
        object_name = f"{dir_name}/{uuid.uuid4()}-{file_name}"
    else:
        object_name = f"{uuid.uuid4()}-{filename}"
    session = aioboto3.Session()
    async with session.client(
        's3',
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region
    ) as s3_client:
        presigned_url = await s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': settings.s3_bucket,
                'Key': object_name,
                'ContentType': content_type
            },
            ExpiresIn=3600
        )
        
        # Determine the final access URL (this depends on the S3 provider and whether bucket is in path or endpoint)
        endpoint = settings.s3_endpoint.rstrip('/')
        if not endpoint.startswith('http'):
            endpoint = f"https://{endpoint}"
            
        access_url = f"{endpoint}/{settings.s3_bucket}/{object_name}"
        
        return {
            "upload_url": presigned_url,
            "access_url": access_url,
            "object_name": object_name
        }
