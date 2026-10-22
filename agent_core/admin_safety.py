"""Bounded data projections for the administration console and retained input."""
import re
from urllib.parse import urlsplit, urlunsplit


def public_endpoint(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return None
        host = '[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname
        return urlunsplit((parsed.scheme, host + (':' + str(parsed.port) if parsed.port else ''), parsed.path, '', ''))
    except (ValueError, TypeError):
        return None


def safe_data(value, depth=0):
    if depth > 12:
        return '[truncated]'
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:100]:
            normalized = re.sub('[^a-z0-9]', '', str(key).lower())
            count = normalized in ('prompttokens', 'completiontokens', 'totaltokens', 'inputtokens', 'outputtokens',
                                   'cachedtokens', 'reasoningtokens', 'maxtokens', 'maxoutputtokens',
                                   'prompttokensdetails', 'completiontokensdetails', 'inputtokensdetails', 'outputtokensdetails')
            sensitive = any(word in normalized for word in ('credential', 'password', 'authorization', 'apikey', 'secret', 'base64'))
            sensitive |= 'token' in normalized and not count
            sensitive |= normalized in ('bytes', 'rawbytes', '_profile')
            sensitive |= normalized == 'data' and ('mediaType' in value or 'mimeType' in value)
            result[str(key)[:200]] = '[redacted]' if sensitive else safe_data(item, depth + 1)
        if len(value) > 100:
            result['_truncated'] = True
        return result
    if isinstance(value, list):
        result = [safe_data(item, depth + 1) for item in value[:100]]
        if len(value) > 100:
            result.append({'_truncated': True})
        return result
    if isinstance(value, str):
        value = re.sub(r'data:[^\s\"\']*;base64,[A-Za-z0-9+/=\s]+', '[redacted image]', value)
        return value[:12000] + ('… [truncated]' if len(value) > 12000 else '')
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return '[redacted]'


def input_summary(body):
    result = {key: safe_data(body[key]) for key in ('messages', 'question') if key in body}
    images = body.get('images', [])
    if isinstance(images, list) and images:
        result['images'] = [safe_data({key: image[key] for key in ('mediaType', 'mimeType', 'width', 'height', 'name') if key in image})
                            for image in images[:32] if isinstance(image, dict)]
    result['available'] = bool(result)
    return result
