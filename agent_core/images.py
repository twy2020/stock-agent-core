"""Only inlined approved image inputs; Core never fetches caller image URLs."""
import base64
import binascii
import copy
import re
from .security import CoreError

MAX_IMAGES=64
MAX_IMAGE_BYTES=8*1024*1024
MAX_TOTAL_BYTES=32*1024*1024


def normalize(value):
    if not isinstance(value,dict): raise CoreError('INVALID_IMAGE')
    media=value.get('mediaType',value.get('mime_type'))
    data=value.get('data',value.get('data_base64'))
    if media not in ('image/png','image/jpeg','image/webp') or not isinstance(data,str): raise CoreError('INVALID_IMAGE')
    try: raw=base64.b64decode(data,validate=True)
    except (ValueError,binascii.Error): raise CoreError('INVALID_IMAGE') from None
    if not raw or len(raw)>MAX_IMAGE_BYTES: raise CoreError('IMAGE_SIZE_LIMIT',413)
    signatures={'image/png':raw.startswith(b'\x89PNG\r\n\x1a\n'),'image/jpeg':raw.startswith(b'\xff\xd8\xff'),
                'image/webp':raw.startswith(b'RIFF') and raw[8:12]==b'WEBP'}
    if not signatures[media]: raise CoreError('INVALID_IMAGE')
    source=value.get('source_ref',value.get('sourceRef',{}))
    if not isinstance(source,dict): raise CoreError('INVALID_IMAGE_SOURCE')
    return {'mediaType':media,'data':data,'source_ref':copy.deepcopy(source)}


def validate_images(values):
    if not isinstance(values,list) or len(values)>MAX_IMAGES: raise CoreError('IMAGE_LIMIT',413)
    images=[normalize(v) for v in values]
    if sum(len(v['data'])*3//4 for v in images)>MAX_TOTAL_BYTES: raise CoreError('IMAGE_SIZE_LIMIT',413)
    return images


def direct_image_message(images,indices=None):
    """Send approved inlined evidence to a VLM, never fetch arbitrary URLs."""
    selected=list(range(len(images))) if indices is None else list(indices)
    parts=[{'type':'text','text':'以下是本次授权图像证据。图像与原生文字可能同源；识读内容仍需工程审核，不得根据像素猜测尺寸。'}]
    for index in selected:
        item=images[index]
        parts.extend([{'type':'text','text':f'图像索引 {index}'},
                      {'type':'image_url','image_url':{'url':f"data:{item['mediaType']};base64,{item['data']}"}}])
    return {'role':'user','content':parts}


def extract_messages(values,images):
    if not isinstance(values,list) or not 1<=len(values)<=200: raise CoreError('INVALID_MESSAGES')
    result=[]
    for message in values:
        if not isinstance(message,dict) or set(message)-{'role','content','name'} or message.get('role') not in ('system','user','assistant'):
            raise CoreError('INVALID_MESSAGES')
        content=message.get('content')
        if isinstance(content,str): result.append({'role':message['role'],'content':content});continue
        if not isinstance(content,list) or message['role']!='user': raise CoreError('INVALID_MESSAGES')
        parts=[]
        for part in content:
            if not isinstance(part,dict): raise CoreError('INVALID_MESSAGES')
            if part.get('type')=='text' and isinstance(part.get('text'),str): parts.append(part['text'])
            elif part.get('type')=='image_url':
                url=(part.get('image_url') or {}).get('url','')
                match=re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)',url) if isinstance(url,str) else None
                if not match: raise CoreError('INLINE_IMAGE_REQUIRED')
                index=len(images)
                images.append(normalize({'mediaType':match[1],'data':match[2],'source_ref':{'messageIndex':len(result),'imageIndex':index}}))
                parts.append(f'[Attached image index {index}; inspect with the OCR tool.]')
            else: raise CoreError('INVALID_MESSAGES')
        result.append({'role':message['role'],'content':'\n'.join(parts)})
    validate_images(images)
    if sum(len(m['content']) for m in result)>500000: raise CoreError('MESSAGE_SIZE_LIMIT',413)
    return result
