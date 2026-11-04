"""Offline checks for the source/target chat pickers."""
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

os.environ.update(BOT_TOKEN='123456:offline-test-token', SUPER_ADMIN_ID='1',
                  SUPABASE_URL='https://example.supabase.co', SUPABASE_KEY='offline-key',
                  TELEGRAM_API_ID='12345', TELEGRAM_API_HASH='offline-hash')

from bot.handlers import admin


class DialogPaginationTests(unittest.IsolatedAsyncioTestCase):
    def picker(self, count, prefix='srcsel', names=None):
        consumed = []
        async def dialogs():
            for index in range(count):
                consumed.append(index)
                yield NS(id=1000 + index, name=names[index] if names else f'Chat {index + 1}',
                         is_user=False, entity=NS(username=None))
        return {'iterator':dialogs(), 'dialogs':[], 'prefix':prefix,
                'page':0, 'exhausted':False, 'bot_id':123}, consumed

    def buttons(self, keyboard):
        return [button for row in keyboard.inline_keyboard for button in row]

    async def test_first_page_shows_ten_and_fetches_only_one_lookahead(self):
        picker, consumed = self.picker(100)
        await admin._load_dialog_page(picker, 0)
        text, keyboard = admin._render_dialog_page(picker, 0)
        self.assertEqual(len(consumed), 11)
        selections = [b for b in self.buttons(keyboard) if b.callback_data.startswith('srcsel:')]
        self.assertEqual([b.text for b in selections], [str(i) for i in range(1, 11)])
        self.assertIn('Showing chats 1–10', text)
        self.assertIn('dialogpage:srcsel:1', [b.callback_data for b in self.buttons(keyboard)])
        self.assertNotIn('11. Chat 11', text)
        self.assertFalse(any('Previous' in b.text for b in self.buttons(keyboard)))

    async def test_next_page_shows_next_ten_and_previous_reuses_cache(self):
        picker, consumed = self.picker(35, 'tgtsel')
        await admin._load_dialog_page(picker, 0)
        await admin._load_dialog_page(picker, 1)
        text, keyboard = admin._render_dialog_page(picker, 1)
        self.assertEqual(len(consumed), 21)
        selections = [b for b in self.buttons(keyboard) if b.callback_data.startswith('tgtsel:')]
        self.assertEqual([b.callback_data for b in selections], [f'tgtsel:{i}' for i in range(10, 20)])
        self.assertIn('Showing chats 11–20', text)
        self.assertIn('dialogpage:tgtsel:0', [b.callback_data for b in self.buttons(keyboard)])
        await admin._load_dialog_page(picker, 0)
        self.assertEqual(len(consumed), 21)

    async def test_last_partial_page_has_no_next_button(self):
        picker, _ = self.picker(21)
        await admin._load_dialog_page(picker, 2)
        text, keyboard = admin._render_dialog_page(picker, 2)
        self.assertIn('Showing chats 21–21', text)
        selections = [b for b in self.buttons(keyboard) if b.callback_data.startswith('srcsel:')]
        self.assertEqual(len(selections), 1)
        self.assertFalse(any('more' in b.text for b in self.buttons(keyboard)))

    async def test_exact_ten_chats_have_no_next_button(self):
        picker, _ = self.picker(10)
        await admin._load_dialog_page(picker, 0)
        _, keyboard = admin._render_dialog_page(picker, 0)
        self.assertTrue(picker['exhausted'])
        self.assertFalse(any(b.callback_data.startswith('dialogpage:') for b in self.buttons(keyboard)))

    async def test_empty_picker_keeps_cancel_button(self):
        picker, _ = self.picker(0)
        await admin._load_dialog_page(picker, 0)
        text, keyboard = admin._render_dialog_page(picker, 0)
        self.assertIn('No channels', text)
        self.assertEqual([b.callback_data for b in self.buttons(keyboard)], ['cancel_action'])

    async def test_chat_titles_are_escaped_and_kept_on_one_line(self):
        picker, _ = self.picker(1, names=['<b>Test & name</b>\nsecond line'])
        await admin._load_dialog_page(picker, 0)
        text, _ = admin._render_dialog_page(picker, 0)
        self.assertIn('&lt;b&gt;Test &amp; name&lt;/b&gt; second line', text)

    async def test_pagination_continues_beyond_old_eighty_chat_limit(self):
        picker, consumed = self.picker(105)
        await admin._load_dialog_page(picker, 8)
        text, keyboard = admin._render_dialog_page(picker, 8)
        self.assertEqual(len(consumed), 91)
        self.assertIn('Showing chats 81–90', text)
        self.assertIn('srcsel:89', [b.callback_data for b in self.buttons(keyboard)])

    async def test_fetch_stores_one_snapshot_and_omits_this_bot(self):
        async def dialogs():
            yield NS(id=123, name='This bot', is_user=True, entity=NS(bot=True, id=123))
            for index in range(12):
                yield NS(id=1000 + index, name=f'Chat {index}', is_user=False, entity=NS(username=None))
        client = NS(iter_dialogs=Mock(return_value=dialogs()))
        user_data = {}
        with patch.object(admin.userbot_manager, 'start_userbot', new_callable=AsyncMock, return_value=client):
            result = await admin._get_dialogs_keyboard_and_text(22, NS(id=123), 'srcsel', user_data)
        self.assertEqual(result[2][0]['id'], 1000)
        self.assertIs(result[2], user_data['dialog_picker']['dialogs'])
        self.assertEqual(len(result[2]), 11)

    async def test_navigation_edits_menu_and_preserves_conversation_state(self):
        for prefix, expected in [('srcsel', admin.ADD_SOURCE_WAIT), ('tgtsel', admin.ADD_TARGET_WAIT)]:
            with self.subTest(prefix=prefix):
                picker, _ = self.picker(25, prefix)
                await admin._load_dialog_page(picker, 0)
                query = NS(data=f'dialogpage:{prefix}:1', from_user=NS(id=22),
                           answer=AsyncMock(), edit_message_text=AsyncMock())
                context = NS(user_data={'dialog_picker':picker, 'temp_dialogs':picker['dialogs']})
                state = await admin.dialog_page_callback.__wrapped__(NS(callback_query=query), context)
                self.assertEqual(state, expected)
                self.assertEqual(picker['page'], 1)
                self.assertIn('Showing chats 11–20', query.edit_message_text.await_args.args[0])
                self.assertEqual(query.edit_message_text.await_args.kwargs['parse_mode'], 'HTML')

    async def test_invalid_page_does_not_fetch_more_or_change_selection(self):
        picker, consumed = self.picker(100)
        await admin._load_dialog_page(picker, 0)
        for data in ['dialogpage:srcsel:99', 'dialogpage:srcsel:-1', 'dialogpage:tgtsel:1', 'bad']:
            query = NS(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
            context = NS(user_data={'dialog_picker':picker})
            state = await admin.dialog_page_callback.__wrapped__(NS(callback_query=query), context)
            self.assertEqual(state, admin.ADD_SOURCE_WAIT)
            query.edit_message_text.assert_not_awaited()
        self.assertEqual(len(consumed), 11)

    async def test_selecting_chat_on_second_page_uses_correct_channel(self):
        picker, _ = self.picker(25)
        await admin._load_dialog_page(picker, 1)
        query = NS(data='srcsel:10', from_user=NS(id=22), answer=AsyncMock(), edit_message_text=AsyncMock())
        context = NS(user_data={'temp_dialogs':picker['dialogs']}, bot=NS())
        with patch.object(admin.channels_db, 'set_source_channel') as save, \
             patch.object(admin.userbot_manager, 'restart_userbot_listener', new_callable=AsyncMock):
            state = await admin.addsource_callback.__wrapped__(NS(callback_query=query), context)
        save.assert_called_once_with(22, 1010, None)
        self.assertEqual(state, admin.ConversationHandler.END)


if __name__ == '__main__':
    unittest.main()
