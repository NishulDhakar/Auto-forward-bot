"""Offline regression tests. All Telegram and Supabase operations are mocked."""
import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

# Never let tests read or use real credentials from .env.
os.environ.update(BOT_TOKEN='123456:offline-test-token', SUPER_ADMIN_ID='1',
                  SUPABASE_URL='https://example.supabase.co', SUPABASE_KEY='offline-key',
                  TELEGRAM_API_ID='12345', TELEGRAM_API_HASH='offline-hash')

from telethon import types
from telethon.errors import ChatWriteForbiddenError
from telegram import Message, Chat, MessageEntity
from telegram.error import RetryAfter
from bot.utils.delivery import OrderedDispatcher, send_with_retry
from bot.utils import userbot_manager as manager
from bot.handlers import forwarding, payment
from bot.webhook import razorpay_webhook as webhook
from bot.utils import scheduler
from bot.db import filters as filters_db, schedules as schedules_db


def source_message(mid=1, text='hello', group=None):
    return NS(id=mid, chat_id=-100123, grouped_id=group, message=text, entities=[],
              file=None, media=None, action=None, date=datetime.now(timezone.utc), web_preview=None)


def bot_message(text='hello', entities=None):
    return Message(message_id=10, date=datetime.now(timezone.utc),
                   chat=Chat(-100123, 'channel'), text=text, entities=entities)


class FilterTests(unittest.TestCase):
    def test_utf16_offset_after_astral_emoji(self):
        original = types.MessageEntityBold(7, 4)
        text, entities = manager._adjust_entities_for_replacement('😀 old text', [original], 'old', 'newword')
        self.assertEqual(text, '😀 newword text')
        self.assertEqual((entities[0].offset, entities[0].length), (11, 4))
        self.assertEqual(original.offset, 7)

    def test_replacement_with_astral_emoji(self):
        text, entities = manager._adjust_entities_for_replacement('old text', [types.MessageEntityBold(4, 4)], 'old', '😀')
        self.assertEqual(text, '😀 text')
        self.assertEqual((entities[0].offset, entities[0].length), (3, 4))

    def test_entity_covering_replacement(self):
        _, entities = manager._adjust_entities_for_replacement('hello old world', [types.MessageEntityBold(0, 15)], 'old', '😀')
        self.assertEqual((entities[0].offset, entities[0].length), (0, 14))

    def test_preserves_pre_language(self):
        _, entities = manager._adjust_entities_for_replacement('old code', [types.MessageEntityPre(4, 4, 'python')], 'old', 'x')
        self.assertEqual(entities[0].language, 'python')
        self.assertEqual(entities[0].offset, 2)

    def test_block_is_distinct_from_empty_replacement(self):
        self.assertEqual(manager._apply_cached_filters_with_entities(
            [{'find_text': '<BLOCK>', 'replace_text': 'BAD'}], 'bad content', []), (None, []))
        self.assertEqual(manager._apply_cached_filters_with_entities(
            [{'find_text': 'hello', 'replace_text': ''}], 'hello', []), ('', []))

    def test_bot_path_preserves_utf16_formatting(self):
        msg = bot_message('😀 old text', [MessageEntity('bold', 7, 4)])
        text, entities = forwarding._filtered(msg, [{'find_text':'old', 'replace_text':'x'}])
        self.assertEqual(text, '😀 x text')
        self.assertEqual(entities[0].offset, 5)

    def test_replacement_is_literal(self):
        text, _ = manager._apply_cached_filters_with_entities(
            [{'find_text':'<ALL_LINKS>', 'replace_text':r'\1'}], 'https://example.com', [])
        self.assertEqual(text, r'\1')


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_and_duplicate_suppression(self):
        queue = OrderedDispatcher(capacity=2)
        output = []
        async def job(n):
            await asyncio.sleep(0)
            output.append(n)
        await queue.submit(10, ('s', 1), lambda: job(1))
        await queue.submit(10, ('s', 2), lambda: job(2))
        self.assertFalse(await queue.submit(10, ('s', 1), lambda: job(99)))
        await queue.queues[10].join()
        self.assertFalse(await queue.submit(10, ('s', 2), lambda: job(99)))
        self.assertEqual(output, [1, 2])
        self.assertEqual(queue.snapshot()['duplicates'], 2)
        await queue.stop(10)
        self.assertFalse(queue.workers)

    async def test_backpressure(self):
        queue = OrderedDispatcher(capacity=1)
        release = asyncio.Event()
        await queue.submit(10, 1, release.wait)
        await asyncio.sleep(0)
        await queue.submit(10, 2, release.wait)
        producer = asyncio.create_task(queue.submit(10, 3, release.wait))
        await asyncio.sleep(0)
        self.assertFalse(producer.done())
        self.assertEqual(queue.queues[10].qsize(), 1)
        release.set()
        await producer
        await queue.stop(10)

    async def test_failures_are_visible_and_not_replayed(self):
        queue = OrderedDispatcher()
        async def fail():
            raise RuntimeError('simulated')
        with self.assertLogs('bot.utils.delivery', level='ERROR'):
            await queue.submit(10, 1, fail)
            await queue.queues[10].join()
        self.assertEqual(queue.snapshot(10)['recent_failures'], 1)
        self.assertFalse(await queue.submit(10, 1, fail))
        await queue.stop(10)

    async def test_account_workers_are_independent(self):
        queue = OrderedDispatcher()
        release = asyncio.Event()
        done = asyncio.Event()
        await queue.submit(10, 1, release.wait)
        await queue.submit(20, 1, AsyncMock(side_effect=done.set))
        await asyncio.wait_for(done.wait(), 1)
        release.set()
        await queue.stop(10)
        await queue.stop(20)

    async def test_only_rate_limit_is_retried(self):
        send = AsyncMock(side_effect=[RetryAfter(1), 'ok'])
        with patch('bot.utils.delivery.asyncio.sleep', new_callable=AsyncMock) as sleep:
            self.assertEqual(await send_with_retry(send), 'ok')
            sleep.assert_awaited_once()
        self.assertEqual(send.await_count, 2)
        send = AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            await send_with_retry(send)
        self.assertEqual(send.await_count, 1)

    async def test_timeout_never_triggers_second_transport(self):
        client = NS(send_message=AsyncMock(side_effect=TimeoutError()))
        with patch.object(manager, '_get_target_entity', new_callable=AsyncMock, return_value=123), \
             patch.object(manager, '_bot_fallback', new_callable=AsyncMock) as fallback:
            with self.assertRaises(TimeoutError):
                await manager._deliver_target(client, NS(), 2, 123, [(source_message(), 'hello', [])])
            fallback.assert_not_awaited()

    async def test_permission_rejection_uses_fallback(self):
        client = NS(send_message=AsyncMock(side_effect=ChatWriteForbiddenError(None)))
        with patch.object(manager, '_get_target_entity', new_callable=AsyncMock, return_value=123), \
             patch.object(manager, '_bot_fallback', new_callable=AsyncMock) as fallback:
            await manager._deliver_target(client, NS(), 2, 123, [(source_message(), 'hello', [])])
            fallback.assert_awaited_once()

    async def test_album_live_and_poll_use_same_key(self):
        queue = OrderedDispatcher()
        with patch.object(manager, '_dispatcher', queue), \
             patch.object(manager, '_deliver_messages', new_callable=AsyncMock) as deliver:
            await manager._enqueue_messages(None, None, 2, -100123, [source_message(3, group=7), source_message(4, group=7)])
            await manager._enqueue_messages(None, None, 2, -100123, [source_message(3, group=7), source_message(4, group=7)])
            await queue.queues[2].join()
            deliver.assert_awaited_once()
            await queue.stop(2)

    async def test_old_message_is_delivered(self):
        msg = source_message()
        msg.date -= timedelta(hours=1)
        with patch.object(manager, '_get_cached_working_status', new_callable=AsyncMock, return_value=True), \
             patch.object(manager, '_get_cached_targets', new_callable=AsyncMock, return_value=[{'channel_id': 123}]), \
             patch.object(manager, '_get_cached_filters', new_callable=AsyncMock, return_value=[]), \
             patch.object(manager, '_deliver_target', new_callable=AsyncMock) as deliver:
            await manager._deliver_messages(None, None, 2, -100123, [msg])
            deliver.assert_awaited_once()

    async def test_bot_block_rule_never_sends_original(self):
        context = NS(bot=NS(send_message=AsyncMock(), copy_message=AsyncMock()))
        with patch.object(manager, 'is_admin_working', new_callable=AsyncMock, return_value=True), \
             patch.object(manager, '_get_cached_targets', new_callable=AsyncMock, return_value=[{'channel_id':123}]), \
             patch.object(manager, '_get_cached_filters', new_callable=AsyncMock,
                          return_value=[{'find_text':'<BLOCK>', 'replace_text':'bad'}]):
            await forwarding._forward_for_admin(context, [bot_message('bad message')], 2)
        context.bot.send_message.assert_not_awaited()
        context.bot.copy_message.assert_not_awaited()

    async def test_bot_empty_replacement_never_restores_original(self):
        context = NS(bot=NS(send_message=AsyncMock(), copy_message=AsyncMock()))
        with patch.object(manager, 'is_admin_working', new_callable=AsyncMock, return_value=True), \
             patch.object(manager, '_get_cached_targets', new_callable=AsyncMock, return_value=[{'channel_id':123}]), \
             patch.object(manager, '_get_cached_filters', new_callable=AsyncMock,
                          return_value=[{'find_text':'hello', 'replace_text':''}]):
            await forwarding._forward_for_admin(context, [bot_message()], 2)
        context.bot.send_message.assert_not_awaited()

    async def test_expired_user_cannot_forward(self):
        manager.invalidate_cache(2)
        with patch.object(manager.users_db, 'get_user', return_value={
            'role':'admin', 'is_working':True,
            'subscription_end':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}):
            self.assertFalse(await manager.is_admin_working(2))
        manager.invalidate_cache(2)

    async def test_expiry_invalidates_even_fresh_cache(self):
        manager._working_cache[2] = {'data':True, 'ts':time.time(), 'expires':time.time()-1}
        with patch.object(manager.users_db, 'get_user', return_value={'role':'user'}):
            self.assertFalse(await manager.is_admin_working(2))
        manager.invalidate_cache(2)

    async def test_pending_auth_disconnect_is_awaited(self):
        client = NS(disconnect=AsyncMock())
        manager.set_pending_auth(2, client, 'unused', 'unused')
        await manager.clear_pending_auth(2)
        client.disconnect.assert_awaited_once()
        self.assertIsNone(manager.get_pending_auth(2))


class OwnershipTests(unittest.TestCase):
    def test_filter_delete_has_tenant_constraint(self):
        client = MagicMock()
        chain = client.table.return_value
        chain.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{'id':'filter'}]
        with patch.object(filters_db, 'get_client', return_value=client):
            filters_db.remove_filter('filter', 22)
        chain.select.return_value.eq.return_value.eq.assert_called_with('admin_id', 22)
        chain.delete.return_value.eq.return_value.eq.assert_called_with('admin_id', 22)

    def test_schedule_delete_has_tenant_constraint(self):
        client = MagicMock()
        chain = client.table.return_value
        chain.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = [{'id':'schedule'}]
        with patch.object(schedules_db, 'get_client', return_value=client):
            self.assertTrue(schedules_db.deactivate_schedule('schedule', 22))
        chain.select.return_value.eq.return_value.eq.assert_called_with('admin_id', 22)
        chain.update.return_value.eq.return_value.eq.assert_called_with('admin_id', 22)


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    def request(self, payload):
        raw = json.dumps(payload).encode()
        signature = hmac.new(b'test-secret', raw, hashlib.sha256).hexdigest()
        return NS(read=AsyncMock(return_value=raw), headers={'X-Razorpay-Signature':signature}, remote='test')

    def payload(self, amount=8800):
        return {'event':'payment.captured', 'payload':{'payment':{'entity':{
            'id':'pay_test', 'order_id':'order_test', 'amount':amount, 'currency':'INR', 'status':'captured',
            'notes':{'telegram_id':'22', 'plan':'1_month'}}}}}

    async def test_missing_secret_fails_closed(self):
        with patch.object(webhook, 'RAZORPAY_WEBHOOK_SECRET', ''):
            self.assertFalse(webhook._verify_signature(b'anything', ''))

    async def test_valid_and_invalid_signature(self):
        with patch.object(webhook, 'RAZORPAY_WEBHOOK_SECRET', 'test-secret'):
            request = self.request({})
            self.assertTrue(webhook._verify_signature(b'{}', request.headers['X-Razorpay-Signature']))
            self.assertFalse(webhook._verify_signature(b'{}', 'incorrect'))

    async def test_non_object_json_rejected(self):
        with patch.object(webhook, 'RAZORPAY_WEBHOOK_SECRET', 'test-secret'):
            response = await webhook.handle_razorpay_webhook(self.request([]))
            self.assertEqual(response.status, 400)

    async def test_underpayment_rejected(self):
        with patch.object(webhook, 'RAZORPAY_WEBHOOK_SECRET', 'test-secret'), \
             patch.object(webhook.subs_db, 'activate_razorpay_payment') as activate:
            response = await webhook.handle_razorpay_webhook(self.request(self.payload(1)))
            self.assertEqual(response.status, 400)
            activate.assert_not_called()

    async def test_duplicate_payment_does_not_notify_again(self):
        result = {'activated':False, 'expires_at':datetime.now(timezone.utc).isoformat()}
        with patch.object(webhook, 'RAZORPAY_WEBHOOK_SECRET', 'test-secret'), \
             patch.object(webhook.subs_db, 'activate_razorpay_payment', return_value=result), \
             patch.object(webhook, '_notify_user', new_callable=AsyncMock) as notify:
            response = await webhook.handle_razorpay_webhook(self.request(self.payload()))
            self.assertEqual(response.status, 200)
            notify.assert_not_awaited()

    async def test_database_failure_can_recover_on_next_webhook(self):
        result = {'activated':True, 'expires_at':datetime.now(timezone.utc).isoformat()}
        with patch.object(webhook, 'RAZORPAY_WEBHOOK_SECRET', 'test-secret'), \
             patch.object(webhook.subs_db, 'activate_razorpay_payment', side_effect=[RuntimeError('DB down'), result]) as activate, \
             patch.object(webhook, '_notify_user', new_callable=AsyncMock) as notify:
            with self.assertLogs('bot.webhook.razorpay_webhook', level='ERROR'):
                first = await webhook.handle_razorpay_webhook(self.request(self.payload()))
            second = await webhook.handle_razorpay_webhook(self.request(self.payload()))
            self.assertEqual((first.status, second.status), (500, 200))
            self.assertEqual(activate.call_count, 2)
            notify.assert_awaited_once()


class PaymentTests(unittest.TestCase):
    def test_paid_link_from_another_user_is_rejected(self):
        client = MagicMock()
        client.payment_link.fetch.return_value = {'status':'paid', 'currency':'INR', 'amount':8800,
                                                  'notes':{'telegram_id':'99', 'plan':'1_month'}}
        with patch.object(payment, '_get_razorpay_client', return_value=client):
            self.assertIsNone(payment._check_razorpay_payment('link', 22, '1_month'))
        client.payment.fetch.assert_not_called()

    def test_captured_payment_verified_with_provider(self):
        client = MagicMock()
        client.payment_link.fetch.return_value = {'status':'paid', 'currency':'INR', 'amount':8800,
            'notes':{'telegram_id':'22', 'plan':'1_month'}, 'payments':[{'payment_id':'pay_test'}]}
        client.payment.fetch.return_value = {'id':'pay_test', 'status':'captured', 'currency':'INR', 'amount':8800}
        with patch.object(payment, '_get_razorpay_client', return_value=client):
            self.assertEqual(payment._check_razorpay_payment('link', 22, '1_month')['id'], 'pay_test')


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_once_schedule_is_not_discarded(self):
        bot = NS(send_message=AsyncMock(side_effect=RuntimeError('delivery failed')))
        with patch.object(manager, 'is_admin_working', new_callable=AsyncMock, return_value=True), \
             patch.object(scheduler, 'get_target_channels', return_value=[{'channel_id':123}]), \
             patch.object(scheduler, 'deactivate_schedule') as deactivate:
            with self.assertLogs('bot.utils.scheduler', level='ERROR'):
                await scheduler._send_scheduled_message(bot, 22, 'sched', 'text', 'once')
            deactivate.assert_not_called()


class AdditionalTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_builds_with_sequential_conversations(self):
        from bot.main import build_application, post_stop
        app = build_application()
        self.assertEqual(app.concurrent_updates, 1)
        self.assertIs(app.post_stop, post_stop)
        self.assertTrue(app.handlers[1])

    async def test_poller_recovers_more_than_five_messages_and_keeps_album(self):
        messages = [source_message(i, group=7 if i in (2, 3) else None) for i in range(1, 13)]
        for msg in messages:
            msg.date -= timedelta(minutes=1)
        async def stream(*args, **kwargs):
            for msg in messages:
                yield msg
        client = NS(get_messages=AsyncMock(return_value=[messages[-1]]), iter_messages=stream)
        manager._clients[2] = client
        manager._poll_cursors[2] = 0
        try:
            with patch.object(manager, '_enqueue_messages', new_callable=AsyncMock) as enqueue, \
                 patch.object(manager.asyncio, 'sleep', new_callable=AsyncMock, side_effect=[None, asyncio.CancelledError()]):
                with self.assertRaises(asyncio.CancelledError):
                    await manager._channel_poller(client, None, 2, -100123)
                batches = [[m.id for m in call.args[-1]] for call in enqueue.await_args_list]
                self.assertEqual(batches, [[1], [2, 3]] + [[i] for i in range(4, 13)])
                self.assertEqual(manager._poll_cursors[2], 12)
        finally:
            manager._clients.pop(2, None)
            manager._poll_cursors.pop(2, None)

    async def test_hidden_link_rule_updates_url_without_mutating_source(self):
        original = types.MessageEntityTextUrl(0, 5, 'https://old.example')
        text, entities = manager._apply_cached_filters_with_entities(
            [{'find_text':'<ALL_LINKS>', 'replace_text':'https://new.example'}], 'click', [original])
        self.assertEqual(text, 'click')
        self.assertEqual(entities[0].url, 'https://new.example')
        self.assertEqual(original.url, 'https://old.example')

    async def test_hidden_link_change_uses_filtered_entities(self):
        original = types.MessageEntityTextUrl(0, 5, 'https://old.example')
        changed = types.MessageEntityTextUrl(0, 5, 'https://new.example')
        msg = source_message(text='click')
        msg.entities = [original]
        client = NS(send_message=AsyncMock())
        with patch.object(manager, '_get_target_entity', new_callable=AsyncMock, return_value=123):
            await manager._deliver_target(client, None, 2, 123, [(msg, 'click', [changed])])
        self.assertEqual(client.send_message.await_args.args[1], 'click')
        self.assertEqual(client.send_message.await_args.kwargs['formatting_entities'][0].url, 'https://new.example')

    async def test_bot_api_album_preserves_group(self):
        from telegram import PhotoSize
        messages = [Message(message_id=i, date=datetime.now(timezone.utc), chat=Chat(-100123, 'channel'),
                            photo=[PhotoSize('file'+str(i), 'unique'+str(i), 10, 10)],
                            media_group_id='album', caption='caption') for i in (1, 2)]
        bot = NS(send_media_group=AsyncMock(), copy_message=AsyncMock())
        with patch.object(manager, 'is_admin_working', new_callable=AsyncMock, return_value=True), \
             patch.object(manager, '_get_cached_targets', new_callable=AsyncMock, return_value=[{'channel_id':123}]), \
             patch.object(manager, '_get_cached_filters', new_callable=AsyncMock, return_value=[]):
            await forwarding._forward_for_admin(NS(bot=bot), messages, 2)
        bot.send_media_group.assert_awaited_once()
        self.assertEqual(len(bot.send_media_group.await_args.kwargs['media']), 2)
        bot.copy_message.assert_not_awaited()

    async def test_transports_share_duplicate_history(self):
        queue = OrderedDispatcher()
        msg = source_message(10)
        with patch.object(manager, '_dispatcher', queue), patch.object(forwarding, '_dispatcher', queue), \
             patch.object(manager, '_deliver_messages', new_callable=AsyncMock) as native, \
             patch.object(forwarding, '_forward_for_admin', new_callable=AsyncMock) as bot_send, \
             patch.object(forwarding, 'get_admins_by_source_channel', return_value=[2]), \
             patch.object(manager, 'is_userbot_running', return_value=False):
            await forwarding._dispatch(NS(), [bot_message()])
            await manager._enqueue_messages(None, None, 2, -100123, [msg])
            await queue.queues[2].join()
            bot_send.assert_awaited_once()
            native.assert_not_awaited()
            await queue.stop(2)

    async def test_session_files_are_private(self):
        import stat
        with tempfile.TemporaryDirectory() as folder, patch.object(manager, '_session_dir', folder), \
             patch('bot.db.supabase_client.get_client'):
            manager.save_session_string(22, 'offline-session')
            mode = stat.S_IMODE(os.stat(os.path.join(folder, 'admin_22.session_str')).st_mode)
            self.assertEqual(mode, 0o600)

    async def test_fresh_cache_avoids_database_calls(self):
        manager._target_cache[22] = {'data':[{'channel_id':123}], 'ts':time.time()}
        with patch.object(manager.channels_db, 'get_target_channels') as read:
            self.assertEqual(await manager._get_cached_targets(22), [{'channel_id':123}])
            read.assert_not_called()
        manager.invalidate_cache(22)



if __name__ == '__main__':
    unittest.main()
