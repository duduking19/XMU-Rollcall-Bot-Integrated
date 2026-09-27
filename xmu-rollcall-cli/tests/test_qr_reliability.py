import json
import threading
import time
from unittest.mock import Mock, patch

import pytest
import requests

from xmu_rollcall.qr_server import QRServer, MAX_SESSIONS
from xmu_rollcall.qr_handler import parse_qr_code_text, submit_qr_rollcall, handle_qr_rollcall

TEXT = json.dumps({'rollcallId':'123', 'data':'synthetic-token'})
RID = 'test-request-0000000001'

@pytest.fixture
def server():
    value = QRServer()
    value.start = lambda: None
    value.public_base_url = 'https://scanner.example.test'
    yield value
    value._workers.shutdown(wait=True)


def make(server, callback=lambda *a, **kw:'success', key=None):
    return server.create_session(300, '123', callback, key)


def post(client, item, text=TEXT, rid=RID):
    return client.post('/submit/'+item.sid, json={'text':text,'request_id':rid})


def finished(server, item):
    for _ in range(100):
        if item.state not in {'submitting','verifying'}:
            return item.state
        time.sleep(.005)
    raise AssertionError('Worker did not finish')


def test_bad_qr_does_not_consume_session(server):
    item=make(server);client=server._app.test_client()
    assert post(client,item,'not-a-qr').status_code==422
    assert post(client,item,json.dumps({'rollcallId':'999','data':'wrong'})).status_code==422
    assert item.state=='waiting'
    assert post(client,item).status_code==202
    assert finished(server,item)=='success'


def test_retry_after_lost_response_and_concurrent_duplicate_is_idempotent(server):
    gate=threading.Event(); called=[]
    def upstream(payload,**kwargs):
        called.append(payload);gate.wait(1);return 'success'
    item=make(server,upstream);client=server._app.test_client()
    try:
        assert post(client,item).status_code==202
        assert post(client,item).status_code==200
        assert post(client,item,rid='test-request-0000000002').status_code==409
    finally:gate.set()
    assert finished(server,item)=='success'
    assert post(client,item).json['state']=='success'
    assert len(called)==1


def test_id_cannot_be_reused_for_another_qr(server):
    item=make(server);client=server._app.test_client();post(client,item)
    assert post(client,item,TEXT+' ').status_code==409


def test_school_failure_allows_new_attempt(server):
    callback=Mock(side_effect=['failed','success']);item=make(server,callback);client=server._app.test_client()
    post(client,item);assert finished(server,item)=='failed'
    assert client.get('/status/'+item.sid).json['retryable']
    assert post(client,item,rid='test-request-0000000002').status_code==202
    assert finished(server,item)=='success';assert callback.call_count==2


def test_unknown_result_can_only_verify_without_repeating_put(server):
    callback=Mock(side_effect=['uncertain','success']);item=make(server,callback);client=server._app.test_client()
    post(client,item);assert finished(server,item)=='uncertain'
    assert post(client,item,rid='test-request-0000000002').status_code==409
    client.post('/verify/'+item.sid,json={});assert finished(server,item)=='success'
    assert callback.call_args.kwargs['verify_only'] is True
    assert callback.call_count==2


def test_expired_session_and_account_dedup(server):
    item=make(server,key=('account',123));assert make(server,key=('account',123)) is item
    item.expires=time.monotonic()-1;client=server._app.test_client()
    assert post(client,item).status_code==410
    replacement=make(server,key=('account',123));assert replacement.sid!=item.sid
    assert client.get('/status/'+item.sid).json['state']=='expired'


def test_capacity_is_bounded(server):
    for _ in range(MAX_SESSIONS):make(server)
    with pytest.raises(RuntimeError):make(server)


def test_endpoint_validation_and_origin(server):
    item=make(server);client=server._app.test_client()
    assert client.post('/submit/'+item.sid,json=['bad']).status_code==400
    assert post(client,item,text={'bad':'type'}).status_code==400
    assert client.post('/submit/'+item.sid,data=TEXT).status_code==415
    assert client.post('/submit/'+item.sid,json={'text':TEXT,'request_id':RID},headers={'Origin':'https://evil.invalid'}).status_code==403
    assert post(client,item,text='x'*25000).status_code==413
    assert client.get('/diag').status_code==404
    assert client.get('/diag/session').status_code==404
    assert client.post('/diag/log',json={}).status_code==404


def test_telemetry_strips_sensitive_fields_and_is_bounded(server,caplog):
    item=make(server);client=server._app.test_client()
    with caplog.at_level('INFO'):
        for _ in range(30):
            assert client.post('/events/'+item.sid,json={'event':'photo_error','decoded':'secret-qr-token','filename':'private.jpg','width':1000}).status_code==200
    assert 'photo_error' in caplog.text and '1000' in caplog.text
    assert 'secret-qr-token' not in caplog.text and 'private.jpg' not in caplog.text and item.sid not in caplog.text
    assert client.post('/events/'+item.sid,json={'event':'photo_error'}).status_code==429


def test_scanner_is_self_contained_and_not_cacheable(server):
    item=make(server);response=server._app.test_client().get('/scan/'+item.sid)
    assert response.status_code==200
    assert b'<script src=' not in response.data
    assert b'webpackUniversalModuleDefinition' in response.data
    assert response.headers['Cache-Control']=='no-store'
    assert "worker-src blob:" in response.headers['Content-Security-Policy']
    assert response.headers['Referrer-Policy']=='no-referrer'
    assert b'"mock": false' in response.data


def reply(code=200, body=None, content_type='application/json'):
    return Mock(status_code=code,headers={'Content-Type':content_type},json=Mock(return_value=body))


def test_200_html_is_not_success_and_timeouts_are_bounded():
    session=Mock();session.put.return_value=reply(content_type='text/html')
    session.get.return_value=reply(content_type='text/html')
    assert submit_qr_rollcall(session,{'rollcallId':'123','data':'test'})=='uncertain'
    for operation in (session.put,session.get):
        assert operation.call_args.kwargs['timeout']==(5,12)
        assert operation.call_args.kwargs['allow_redirects'] is False


def test_matching_school_record_is_required():
    session=Mock();session.put.return_value=reply(body={})
    session.get.return_value=reply(body={'rollcalls':[{'rollcall_id':999,'status':'on_call_fine'}]})
    assert submit_qr_rollcall(session,{'rollcallId':'123','data':'test'})=='uncertain'
    session.get.return_value=reply(body={'rollcalls':[{'rollcall_id':123,'status':'on_call_fine'}]})
    assert submit_qr_rollcall(session,{'rollcallId':'123','data':'test'})=='success'


def test_timeout_verifies_without_resubmitting():
    session=Mock();session.put.side_effect=requests.Timeout()
    session.get.return_value=reply(body={'rollcalls':[{'rollcall_id':123,'status':'on_call_fine'}]})
    assert submit_qr_rollcall(session,{'rollcallId':'123','data':'test'})=='success'
    assert session.put.call_count==1


@pytest.mark.parametrize('payload', [
    {'rollcallId':True,'data':'token'}, {'rollcallId':'../x','data':'token'},
    {'rollcallId':123,'data':{}}, {'rollcallId':123,'data':''}])
def test_invalid_payload_types(payload):
    with pytest.raises(ValueError):parse_qr_code_text(json.dumps(payload))


def test_scheduling_does_not_wait_for_scan_and_deduplicates(server):
    config={'enabled':True,'session_timeout':300,'ngrok_token':'fake'}
    notifier=Mock();notifier.send_qr_link.return_value.ok=True
    with patch('xmu_rollcall.qr_handler.get_qr_config',return_value=config), \
         patch('xmu_rollcall.qr_handler.load_config',return_value={}), \
         patch('xmu_rollcall.qr_handler.build_notifier',return_value=notifier), \
         patch('xmu_rollcall.qr_handler._get_server',return_value=server):
        session=requests.Session();rollcall={'rollcall_id':123,'account_id':1}
        assert handle_qr_rollcall(session,rollcall) is None
        assert handle_qr_rollcall(session,rollcall) is None
    assert len(server._sessions)==1
    assert notifier.send_qr_link.call_count==1
    notifier.send_rollcall_success.assert_not_called()


def test_notification_failure_is_retried_without_new_session(server):
    item=make(server,key=('acct',123))
    assert server.claim_notice(item)
    assert not server.claim_notice(item)
    item.next_notice=0
    assert server.claim_notice(item)
    server.notice_delivered(item)
    item.next_notice=0
    assert not server.claim_notice(item)
    assert make(server,key=('acct',123)) is item


def test_queued_submission_is_not_sent_after_expiry(server):
    callback=Mock();item=make(server,callback)
    item.expires=time.monotonic()-1
    server._run(item,{'rollcallId':'123','data':'synthetic'},False)
    assert item.state=='expired';callback.assert_not_called()


def test_bad_event_type_is_rejected(server):
    item=make(server)
    assert server._app.test_client().post('/events/'+item.sid,json={'event':['invalid']}).status_code==400


@pytest.mark.parametrize('origin',['http://example.test','https://user:password@example.test','https://example.test/scan','https://example.test?token=x'])
def test_public_origin_configuration_is_strict(origin):
    with pytest.raises(ValueError):QRServer(public_base_url=origin)


def test_dedup_is_account_scoped(server):
    first=make(server,key=(1,123));second=make(server,key=(2,123))
    assert first.sid!=second.sid


def test_refresh_pending_qr_does_not_repeat_number_or_radar():
    from xmu_rollcall.rollcall_handler import refresh_pending_qr
    from test_rollcall_handler import rollcall
    data={'rollcalls':[rollcall(),rollcall(is_number=True),rollcall(is_radar=True),rollcall(status='on_call_fine')]}
    with patch('xmu_rollcall.rollcall_handler.handle_qr_rollcall') as handler:
        refresh_pending_qr(data,Mock(),account={'id':7,'name':'synthetic'})
    assert handler.call_count==1
    assert handler.call_args.args[1]['account_id']==7
