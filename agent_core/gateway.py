"""Bounded OpenAI-compatible gateway; caller model names are approved profile IDs."""
import copy
import json
import math
import re
import threading
import uuid

import httpx
from flask import Response, g, jsonify, request, stream_with_context
from .config import model_profile
from .images import normalize, validate_images
from .model import thinking_control
from .providers import bounded_json, clean_usage, init_usage, record_usage, upstream_status
from .security import CoreError, authenticate, credential, endpoint


def prepare_payload(value, service, profile):
    allowed = {'model', 'messages', 'stream', 'stream_options', 'temperature', 'top_p', 'max_tokens', 'max_completion_tokens',
               'stop', 'tools', 'tool_choice', 'parallel_tool_calls', 'response_format', 'seed', 'frequency_penalty', 'presence_penalty', 'reasoning_enabled'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise CoreError('UNKNOWN_REQUEST_FIELD')
    messages = value.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 200:
        raise CoreError('INVALID_MESSAGES')
    images = []; chars = 0
    for message in messages:
        if not isinstance(message, dict) or set(message) - {'role', 'content', 'name', 'tool_call_id', 'tool_calls'} or message.get('role') not in ('system', 'developer', 'user', 'assistant', 'tool'):
            raise CoreError('INVALID_MESSAGES')
        content = message.get('content')
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list) and message['role'] == 'user':
            for part in content:
                if not isinstance(part, dict):
                    raise CoreError('INVALID_MESSAGES')
                if part.get('type') == 'text' and set(part) == {'type', 'text'} and isinstance(part.get('text'), str):
                    chars += len(part['text'])
                elif part.get('type') == 'image_url' and set(part) == {'type', 'image_url'}:
                    image = part['image_url']
                    if not isinstance(image, dict) or set(image) - {'url', 'detail'} or image.get('detail', 'auto') not in ('auto', 'low', 'high'):
                        raise CoreError('INVALID_IMAGE')
                    url = image.get('url', '')
                    match = re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)', url) if isinstance(url, str) else None
                    if not match:
                        raise CoreError('INLINE_IMAGE_REQUIRED')
                    images.append(normalize({'mediaType': match[1], 'data': match[2]}))
                else:
                    raise CoreError('INVALID_MESSAGES')
        elif not (content is None and message['role'] == 'assistant' and message.get('tool_calls')):
            raise CoreError('INVALID_MESSAGES')
        for key in ('name', 'tool_call_id'):
            if key in message and (not isinstance(message[key], str) or not 1 <= len(message[key]) <= 200):
                raise CoreError('INVALID_MESSAGES')
        if 'tool_calls' in message:
            if message['role'] != 'assistant' or not isinstance(message['tool_calls'], list) or len(message['tool_calls']) > 32:
                raise CoreError('INVALID_MESSAGES')
            for call in message['tool_calls']:
                if not isinstance(call, dict) or set(call) - {'id', 'type', 'function'} or call.get('type') != 'function' or not isinstance(call.get('id'), str):
                    raise CoreError('INVALID_MESSAGES')
                function = call.get('function')
                if not isinstance(function, dict) or set(function) != {'name', 'arguments'} or any(not isinstance(v, str) for v in function.values()):
                    raise CoreError('INVALID_MESSAGES')
                chars += len(function['arguments'])
    if chars > 500000:
        raise CoreError('MESSAGE_SIZE_LIMIT', 413)
    validate_images(images)
    if images and (service.get('visionPolicy', 'ocr') not in ('direct', 'both') or not profile.get('capabilities', {}).get('vision')):
        raise CoreError('DIRECT_VISION_FORBIDDEN', 403)
    if type(value.get('stream', False)) is not bool:
        raise CoreError('INVALID_STREAM')
    options = value.get('stream_options', {})
    if not isinstance(options, dict) or set(options) - {'include_usage'} or ('include_usage' in options and type(options['include_usage']) is not bool):
        raise CoreError('INVALID_STREAM')
    payload = copy.deepcopy(value); payload['model'] = profile['model']; payload['stream'] = value.get('stream', False)
    payload.pop('stream_options', None)
    if payload['stream']:
        payload['stream_options'] = {'include_usage': True}
    for key, low, high in (('temperature', 0, 2), ('top_p', 0, 1), ('frequency_penalty', -2, 2), ('presence_penalty', -2, 2)):
        if key in value and (type(value[key]) not in (int, float) or not math.isfinite(value[key]) or not low <= value[key] <= high):
            raise CoreError('INVALID_GENERATION_OPTION')
    if 'seed' in value and (type(value['seed']) is not int or not -(2**63) <= value['seed'] < 2**63):
        raise CoreError('INVALID_GENERATION_OPTION')
    if 'max_tokens' in value and 'max_completion_tokens' in value:
        raise CoreError('INVALID_TOKEN_LIMIT')
    cap = min(8192, service.get('maxOutputTokens', 4096), profile.get('maxOutputTokens', 8192))
    key = 'max_completion_tokens' if 'max_completion_tokens' in value else 'max_tokens'
    count = value.get(key, cap)
    if type(count) is not int or not 1 <= count <= cap:
        raise CoreError('INVALID_TOKEN_LIMIT')
    payload[key] = count
    if 'stop' in value:
        stops = [value['stop']] if isinstance(value['stop'], str) else value['stop']
        if not isinstance(stops, list) or not 1 <= len(stops) <= 4 or any(not isinstance(v, str) or len(v) > 256 for v in stops):
            raise CoreError('INVALID_GENERATION_OPTION')
    if 'tools' in value:
        if profile.get('capabilities',{}).get('tools') is False:
            raise CoreError('MODEL_TOOLS_UNSUPPORTED')
        if not isinstance(value['tools'], list) or not 1 <= len(value['tools']) <= 32 or len(json.dumps(value['tools'])) > 65536:
            raise CoreError('INVALID_TOOLS')
        for tool in value['tools']:
            if not isinstance(tool, dict) or set(tool) != {'type', 'function'} or tool['type'] != 'function' or not isinstance(tool['function'], dict):
                raise CoreError('INVALID_TOOLS')
            function = tool['function']
            if set(function) - {'name', 'description', 'parameters', 'strict'} or not isinstance(function.get('name'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', function['name']):
                raise CoreError('INVALID_TOOLS')
            if 'parameters' in function and not isinstance(function['parameters'], dict):
                raise CoreError('INVALID_TOOLS')
    if 'tool_choice' in value:
        choice = value['tool_choice']
        if isinstance(choice, dict):
            valid = set(choice) == {'type', 'function'} and choice['type'] == 'function' and isinstance(choice['function'], dict) and set(choice['function']) == {'name'} and choice['function']['name'] in [v['function']['name'] for v in value.get('tools', [])]
        else:
            valid = choice in ('auto', 'none', 'required')
        if not valid:
            raise CoreError('INVALID_TOOLS')
    if 'parallel_tool_calls' in value and type(value['parallel_tool_calls']) is not bool:
        raise CoreError('INVALID_TOOLS')
    if 'response_format' in value:
        fmt = value['response_format']
        if not isinstance(fmt, dict) or set(fmt) != {'type'} or fmt['type'] not in ('text', 'json_object'):
            raise CoreError('INVALID_RESPONSE_FORMAT')
    reasoning = payload.pop('reasoning_enabled', None)
    if reasoning is not None:
        if type(reasoning) is not bool:
            raise CoreError('INVALID_REASONING')
        control = thinking_control(profile.get('providerType', 'OPENAI'), profile['model'])
        if not control:
            raise CoreError('THINKING_UNSUPPORTED')
        payload[control] = {'type': 'enabled' if reasoning else 'disabled'} if control == 'thinking' else reasoning
    return payload


def public_completion(value, profile_id, streaming):
    if not isinstance(value, dict) or 'error' in value or not isinstance(value.get('choices'), list) or len(value['choices']) > 1:
        raise CoreError('INVALID_UPSTREAM_RESPONSE', 502)
    choices = []
    for choice in value['choices']:
        field = 'delta' if streaming else 'message'
        if not isinstance(choice, dict) or not isinstance(choice.get(field), dict):
            raise CoreError('INVALID_UPSTREAM_RESPONSE', 502)
        if choice.get('finish_reason') not in (None, 'stop', 'length', 'tool_calls', 'content_filter', 'function_call'):
            raise CoreError('INVALID_UPSTREAM_RESPONSE', 502)
        for name in ('content', 'reasoning_content', 'refusal', 'role'):
            if choice[field].get(name) is not None and not isinstance(choice[field][name], str):
                raise CoreError('INVALID_UPSTREAM_RESPONSE', 502)
        if 'tool_calls' in choice[field] and not isinstance(choice[field]['tool_calls'], list):
            raise CoreError('INVALID_UPSTREAM_RESPONSE', 502)
        choices.append({'index': 0, field: {k: v for k, v in choice[field].items() if k in ('role', 'content', 'reasoning_content', 'tool_calls', 'refusal')}, 'finish_reason': choice.get('finish_reason')})
    result = {'id': value.get('id', ''), 'object': 'chat.completion.chunk' if streaming else 'chat.completion',
              'created': value.get('created', 0), 'model': profile_id, 'choices': choices}
    if isinstance(value.get('usage'), dict):
        result['usage'] = clean_usage(value['usage'])
    return result


def _sse_values(response):
    pending = b''; total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > 16 * 1024 * 1024:
            raise CoreError('UPSTREAM_RESPONSE_LIMIT', 502)
        pending += chunk
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            if len(line) > 2 * 1024 * 1024:
                raise CoreError('UPSTREAM_RESPONSE_LIMIT', 502)
            if line.startswith(b'data:'):
                yield line[5:].strip()
        if len(pending) > 2 * 1024 * 1024:
            raise CoreError('UPSTREAM_RESPONSE_LIMIT', 502)
    if pending.startswith(b'data:'):
        yield pending[5:].strip()


def register_gateway(app, config, store):
    init_usage(store)
    lock = threading.RLock(); active = {}
    app.extensions['core_gateway_state'] = {'lock': lock, 'active': active}

    @app.get('/v1/models')
    def gateway_models():
        items = []
        with lock:
            service = authenticate(config, request.headers.get('Authorization', ''))
            for identifier in service.get('modelProfiles', []):
                try:
                    profile = model_profile(config, service, identifier)
                except CoreError:
                    continue
                items.append({'id': profile['id'], 'object': 'model', 'created': 0, 'owned_by': profile.get('providerId', 'agent-core'),
                              **{k: profile[k] for k in ('capabilities', 'contextWindow', 'maxOutputTokens') if k in profile}})
        return jsonify({'object': 'list', 'data': items})

    @app.post('/v1/chat/completions')
    def gateway_completion():
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or not isinstance(value.get('model'), str):
            raise CoreError('INVALID_REQUEST')
        with lock:
            # Authentication can precede a concurrent admin write; resolve the live
            # caller and all profile references again under the mutation lock.
            service = authenticate(config, request.headers.get('Authorization', ''))
            profile = model_profile(config, service, value['model'])
            payload = prepare_payload(value, service, profile)
            key = credential(profile, 'apiKey'); url = endpoint(profile['baseUrl']) + '/chat/completions'
            caller = (service['id'], service['tenantId']); request_id = str(uuid.uuid4())
            if active.get(caller, 0) >= min(8, max(1, service.get('maxActiveRuns', 2))):
                raise CoreError('GATEWAY_LIMIT', 429)
            active[caller] = active.get(caller, 0) + 1
        usage = {}; status = 'failed'

        def exchange():
            nonlocal usage, status
            try:
                with httpx.stream('POST', url, headers={'Authorization': 'Bearer ' + key, 'Accept': 'text/event-stream' if payload['stream'] else 'application/json'},
                                  json=payload, timeout=httpx.Timeout(120, connect=10), follow_redirects=False, trust_env=False,proxy=profile.get('proxyUrl')) as response:
                    upstream_status(response.status_code)
                    if not payload['stream']:
                        result = public_completion(bounded_json(response), profile['id'], False)
                        usage = result.get('usage', {})
                        if not result['choices'] or result['choices'][0]['finish_reason'] is None:
                            raise CoreError('INCOMPLETE_UPSTREAM_RESPONSE', 502)
                        status = 'completed'; yield result; return
                    finished = False
                    for data in _sse_values(response):
                        if not service.get('enabled', True):
                            raise CoreError('SERVICE_REVOKED', 403)
                        model_profile(config, service, profile['id'])
                        if data == b'[DONE]':
                            break
                        if not data:
                            continue
                        try:
                            result = public_completion(json.loads(data), profile['id'], True)
                        except (ValueError, UnicodeError):
                            raise CoreError('INVALID_UPSTREAM_RESPONSE', 502) from None
                        if finished and any(c['delta'] for c in result['choices']):
                            raise CoreError('INVALID_STREAM_AFTER_FINISH', 502)
                        finished = finished or any(c['finish_reason'] is not None for c in result['choices'])
                        usage.update(result.get('usage', {}))
                        yield 'data: ' + json.dumps(result, ensure_ascii=False) + '\n\n'
                    if not finished:
                        raise CoreError('INCOMPLETE_UPSTREAM_RESPONSE', 502)
                    status = 'completed'; yield 'data: [DONE]\n\n'
            except httpx.HTTPError:
                raise CoreError('UPSTREAM_CONNECTION_FAILED', 502) from None
            except GeneratorExit:
                if status != 'completed': status = 'disconnected'
                raise
            finally:
                with lock:
                    active[caller] -= 1
                record_usage(store, service, profile, usage, status, request_id=request_id)

        iterator = exchange()
        if not payload['stream']:
            try:
                result = next(iterator)
            finally:
                iterator.close()
            return jsonify(result)
        first = next(iterator)

        def stream():
            try:
                yield first
                yield from iterator
            except CoreError as exc:
                yield 'data: ' + json.dumps({'error': {'message': exc.code, 'type': 'upstream_error', 'code': exc.code}}) + '\n\n'
            finally:
                iterator.close()
        response = Response(stream_with_context(stream()), mimetype='text/event-stream', headers={'X-Accel-Buffering': 'no'})
        response.call_on_close(iterator.close)
        return response
