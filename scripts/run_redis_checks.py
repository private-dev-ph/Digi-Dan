"""Run actual Lua and adapter acceptance against the disposable loopback Redis."""
import argparse
import concurrent.futures
import json
import socket
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.core.config import Settings
from api.endpoint.application import create_app
from api.query import rag
from api.tools.rate_limit import RedisRateLimiter, RateLimitUnavailable


PORT = None
PREFIX = 'digi-dan:ratelimit:test:chat:v1:'
IDENTITY = 'a' * 64
RESULTS = []


class RedisError(Exception):
    pass


def read_resp(stream):
    line = stream.readline()
    if not line.endswith(b'\r\n'):
        raise RuntimeError('Incomplete local Redis response')
    kind, value = line[:1], line[1:-2]
    if kind == b'+':
        return value.decode()
    if kind == b'-':
        raise RedisError(value.decode())
    if kind == b':':
        return int(value)
    if kind == b'$':
        length = int(value)
        if length == -1:
            return None
        result = stream.read(length)
        assert stream.read(2) == b'\r\n'
        return result.decode()
    if kind == b'*':
        return [read_resp(stream) for _ in range(int(value))]
    raise RuntimeError('Unexpected local Redis response')


def send(stream, args):
    values = [str(value).encode() for value in args]
    stream.write(b'*' + str(len(values)).encode() + b'\r\n')
    for value in values:
        stream.write(b'$' + str(len(value)).encode() + b'\r\n' + value + b'\r\n')
    stream.flush()
    return read_resp(stream)


def command(*args, auth=None):
    with socket.create_connection(('127.0.0.1', PORT), timeout=3) as sock:
        with sock.makefile('rwb') as stream:
            if auth:
                send(stream, ('AUTH', *auth))
            return send(stream, args)


def store(environment='test', auth=None):
    def handler(request):
        payload = json.loads(request.content)
        try:
            result = command(*payload, auth=auth)
            return httpx.Response(200, json={'result':result})
        except RedisError:
            return httpx.Response(200, json={'error':'synthetic Redis rejection'})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return RedisRateLimiter('https://local-test.upstash.io', 'synthetic-token', environment, client=client), client


def record(name, **details):
    RESULTS.append({'check':name, 'passed':True, **details})


def main():
    global PORT
    parser = argparse.ArgumentParser(description="Acceptance against a NEW disposable Redis bound to 127.0.0.1. Never use a shared development database.")
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535')
    PORT = args.port
    assert command('PING') == 'PONG'
    assert command('DBSIZE') == 0, 'Refusing to modify a nonempty Redis database; start a new disposable container'
    assert command('ACL', 'GETUSER', 'local-limiter') is None, 'Refusing to replace an existing ACL user'
    sentinel = 'digi-dan:knowledge:production:registry'
    command('SET', sentinel, 'untouched synthetic knowledge sentinel')
    first, first_client = store()
    second, second_client = store()
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(lambda i: (first if i % 2 else second).consume(IDENTITY, 10, 30), range(40)))
    assert sum(d.allowed for d in decisions) == 10
    assert command('GET', PREFIX + IDENTITY) == '10'
    record('atomic admission across two independent adapters', attempted=40, admitted=10, denied=30)
    ttl = command('PTTL', PREFIX + IDENTITY)
    assert not first.consume(IDENTITY, 10, 30).allowed
    assert 0 < command('PTTL', PREFIX + IDENTITY) <= ttl
    assert command('GET', PREFIX + IDENTITY) == '10'
    record('denied calls preserve count and expiry')
    assert first.consume('b' * 64, 10, 30).allowed
    production, production_client = store('production')
    assert production.consume(IDENTITY, 10, 30).remaining == 9
    record('client and environment counters are separate')
    assert first.consume('c' * 64, 1, 1).allowed
    assert not second.consume('c' * 64, 1, 1).allowed
    time.sleep(1.1)
    assert second.consume('c' * 64, 1, 1).allowed
    record('actual Redis expiry restores allowance')

    for index, value, ttl_ms in [('d','1',None), ('e','invalid',10000), ('f','1',604810000)]:
        key = PREFIX + index * 64
        if ttl_ms is None:
            command('SET',key,value)
        else:
            command('SET',key,value,'PX',ttl_ms)
        before = command('GET',key)
        try:
            first.consume(index * 64,10,30)
        except RateLimitUnavailable:
            pass
        else:
            raise AssertionError('Corrupt state admitted a request')
        assert command('GET',key) == before
    record('corrupt counters and expiries fail closed without mutation')

    # Local-only credential; verify the script works with a narrow command/key ACL.
    auth = ('local-limiter','synthetic-local-password')
    command('ACL','SETUSER',auth[0],'reset','on','>'+auth[1],
            '~'+PREFIX+'*','+eval','+get','+set','+incr','+pttl')
    restricted, restricted_client = store(auth=auth)
    assert restricted.consume('1' * 64,2,30).allowed
    assert restricted.consume('1' * 64,2,30).allowed
    assert not restricted.consume('1' * 64,2,30).allowed
    for forbidden_command in [('SET',sentinel,'forbidden'), ('EVAL',"return redis.call('SET', KEYS[1], 'forbidden')",1,sentinel)]:
        try:
            command(*forbidden_command,auth=auth)
        except RedisError:
            pass
        else:
            raise AssertionError('Restricted credential modified knowledge key')
    record('restricted local ACL permits limiter and rejects knowledge writes')

    class Knowledge:
        def retrieve(self,query,top_k):
            return {'version_id':'local-redis-acceptance'}, [{'id':'fact','text':'Synthetic fact'}]
    settings = Settings(knowledge_mode='versioned',chat_rate_limit_backend='redis',
                        chat_client_identity_mode='trusted_proxy',api_bypass_secret='local-secret',
                        chat_rate_limit=4,chat_rate_limit_window_seconds=30)
    headers = {'x-api-secret':'local-secret','x-digidan-client-id':'2'*64}
    with patch.object(rag,'generate_answer',return_value='Synthetic answer') as generate:
        with TestClient(create_app(settings=settings,knowledge_service=Knowledge(),rate_limiter=first)) as app_a:
            with TestClient(create_app(settings=settings,knowledge_service=Knowledge(),rate_limiter=second)) as app_b:
                responses = [(app_a if i % 2 else app_b).post('/api/chat',json={'message':'test'},headers=headers) for i in range(5)]
    assert [r.status_code for r in responses] == [200,200,200,200,429]
    assert int(responses[-1].headers['retry-after']) > 0
    assert generate.call_count == 4
    record('two actual FastAPI apps share one allowance', admitted=4,denied=1)
    assert command('GET',sentinel) == 'untouched synthetic knowledge sentinel'
    record('knowledge sentinel preserved')
    for client in (first_client,second_client,production_client,restricted_client):
        client.close()
    report={'passed':True,'real_redis':True,'loopback_only':True,'checks':RESULTS,'external_calls':0}
    if args.report:
        args.report.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()



