from weakref import ref
from decimal import Decimal
import re
import datetime
import traceback, sys
import threading

from kivy.app import App
from kivy.cache import Cache
from kivy.clock import Clock
from kivy.compat import string_types
from kivy.properties import (ObjectProperty, DictProperty, NumericProperty,
                             ListProperty, StringProperty)

from kivy.uix.label import Label

from kivy.lang import Builder
from kivy.factory import Factory
from kivy.utils import platform

from electrum_ltc.util import profiler, parse_URI, format_time
from electrum_ltc import bitcoin
from electrum_ltc.util import timestamp_to_datetime
from electrum_ltc.plugins import run_hook
from electrum_ltc.paymentrequest import PR_UNPAID, PR_PAID, PR_UNKNOWN, PR_EXPIRED

from context_menu import ContextMenu


from electrum_ltc_gui.kivy.i18n import _

class EmptyLabel(Factory.Label):
    pass

class CScreen(Factory.Screen):
    __events__ = ('on_activate', 'on_deactivate', 'on_enter', 'on_leave')
    action_view = ObjectProperty(None)
    loaded = False
    kvname = None
    context_menu = None
    menu_actions = []
    app = App.get_running_app()

    def _change_action_view(self):
        app = App.get_running_app()
        action_bar = app.root.manager.current_screen.ids.action_bar
        _action_view = self.action_view

        if (not _action_view) or _action_view.parent:
            return
        action_bar.clear_widgets()
        action_bar.add_widget(_action_view)

    def on_enter(self):
        # FIXME: use a proper event don't use animation time of screen
        Clock.schedule_once(lambda dt: self.dispatch('on_activate'), .25)
        pass

    def update(self):
        pass

    @profiler
    def load_screen(self):
        self.screen = Builder.load_file('gui/kivy/uix/ui_screens/' + self.kvname + '.kv')
        self.add_widget(self.screen)
        self.loaded = True
        self.update()
        setattr(self.app, self.kvname + '_screen', self)

    def on_activate(self):
        if self.kvname and not self.loaded:
            self.load_screen()
        #Clock.schedule_once(lambda dt: self._change_action_view())

    def on_leave(self):
        self.dispatch('on_deactivate')

    def on_deactivate(self):
        self.hide_menu()

    def hide_menu(self):
        if self.context_menu is not None:
            self.remove_widget(self.context_menu)
            self.context_menu = None

    def show_menu(self, obj):
        self.hide_menu()
        self.context_menu = ContextMenu(obj, self.menu_actions)
        self.add_widget(self.context_menu)


class HistoryScreen(CScreen):

    tab = ObjectProperty(None)
    kvname = 'history'

    def __init__(self, **kwargs):
        self.ra_dialog = None
        super(HistoryScreen, self).__init__(**kwargs)
        self.menu_actions = [ ('Label', self.label_dialog), ('Details', self.app.tx_details_dialog)]

    def label_dialog(self, obj):
        from dialogs.label_dialog import LabelDialog
        key = obj.tx_hash
        text = self.app.wallet.get_label(key)
        def callback(text):
            self.app.wallet.set_label(key, text)
            self.update()
        d = LabelDialog(_('Enter Transaction Label'), text, callback)
        d.open()


    def parse_history(self, items):
        for item in items:
            tx_hash, conf, value, timestamp, balance = item
            time_str = _("unknown")
            if conf > 0:
                try:
                    time_str = datetime.datetime.fromtimestamp(timestamp).isoformat(' ')[:-3]
                except Exception:
                    time_str = _("error")
            if conf == -1:
                time_str = _('unverified')
                icon = "atlas://gui/kivy/theming/light/close"
            elif conf == 0:
                time_str = _('pending')
                icon = "atlas://gui/kivy/theming/light/unconfirmed"
            elif conf < 6:
                conf = max(1, conf)
                icon = "atlas://gui/kivy/theming/light/clock{}".format(conf)
            else:
                icon = "atlas://gui/kivy/theming/light/confirmed"

            label = self.app.wallet.get_label(tx_hash) if tx_hash else _('Pruned transaction outputs')
            date = timestamp_to_datetime(timestamp)
            rate = run_hook('history_rate', date)
            if self.app.fiat_unit:
                s = run_hook('historical_value_str', value, date)
                quote_text = "..." if s is None else s + ' ' + self.app.fiat_unit
            else:
                quote_text = ''
            yield (conf, icon, time_str, label, value, tx_hash, quote_text)

    def update(self, see_all=False):
        if self.app.wallet is None:
            return

        history = self.parse_history(reversed(
            self.app.wallet.get_history(self.app.current_account)))
        # repopulate History Card
        history_card = self.screen.ids.history_container
        history_card.clear_widgets()
        count = 0
        for item in history:
            count += 1
            conf, icon, date_time, message, value, tx, quote_text = item
            ri = Factory.HistoryItem()
            ri.icon = icon
            ri.date = date_time
            ri.message = message
            ri.value = value
            ri.quote_text = quote_text
            ri.confirmations = conf
            ri.tx_hash = tx
            ri.screen = self
            history_card.add_widget(ri)
            if count == 8 and not see_all:
                break

        if count == 0:
            msg = _('This screen shows your list of transactions. It is currently empty.')
            history_card.add_widget(EmptyLabel(text=msg))


class SendScreen(CScreen):

    kvname = 'send'
    payment_request = None

    def set_URI(self, uri):
        self.screen.address = uri.get('address', '')
        self.screen.message = uri.get('message', '')
        amount = uri.get('amount')
        if amount:
            self.screen.amount = self.app.format_amount_and_units(amount)

    def update(self):
        if self.app.current_invoice:
            self.set_request(self.app.current_invoice)

    def do_clear(self):
        self.screen.amount = ''
        self.screen.message = ''
        self.screen.address = ''
        self.payment_request = None

    def set_request(self, pr):
        self.payment_request = pr
        self.screen.address = pr.get_requestor()
        amount = pr.get_amount()
        if amount:
            self.screen.amount = self.app.format_amount_and_units(amount)
        self.screen.message = pr.get_memo()

    def do_save(self):
        if not self.screen.address:
            return
        if self.payment_request:
            # it sould be already saved
            return
        # save address as invoice
        from electrum_ltc.paymentrequest import make_unsigned_request, PaymentRequest
        req = {'address':self.screen.address, 'memo':self.screen.message}
        amount = self.app.get_amount(self.screen.amount) if self.screen.amount else 0
        req['amount'] = amount
        pr = make_unsigned_request(req).SerializeToString()
        pr = PaymentRequest(pr)
        self.app.invoices.add(pr)
        self.app.update_tab('invoices')
        self.app.show_info(_("Invoice saved"))

    def do_paste(self):
        contents = unicode(self.app._clipboard.paste())
        if not contents:
            self.app.show_info(_("Clipboard is empty"))
            return
        try:
            uri = parse_URI(contents)
        except:
            self.app.show_info(_("Clipboard content is not a Litecoin URI"))
            return
        self.set_URI(uri)

    def do_send(self):
        if self.payment_request:
            if self.payment_request.has_expired():
                self.app.show_error(_('Payment request has expired'))
                return
            outputs = self.payment_request.get_outputs()
        else:
            address = str(self.screen.address)
            if not bitcoin.is_address(address):
                self.app.show_error(_('Invalid Litecoin Address') + ':\n' + address)
                return
            try:
                amount = self.app.get_amount(self.screen.amount)
            except:
                self.app.show_error(_('Invalid amount') + ':\n' + self.screen.amount)
                return
            outputs = [(bitcoin.TYPE_ADDRESS, address, amount)]
        message = unicode(self.screen.message)
        fee = None
        self.app.protected(self.send_tx, (outputs, fee, message))

    def send_tx(self, *args):
        self.app.show_info("Sending...")
        threading.Thread(target=self.send_tx_thread, args=args).start()

    def send_tx_thread(self, outputs, fee, label, password):
        # make unsigned transaction
        coins = self.app.wallet.get_spendable_coins()
        try:
            tx = self.app.wallet.make_unsigned_transaction(coins, outputs, self.app.electrum_config, fee)
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            self.app.show_error(str(e))
            return
        # sign transaction
        try:
            self.app.wallet.sign_transaction(tx, password)
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            self.app.show_error(str(e))
            return
        if not tx.is_complete():
            self.app.show_info("Transaction is not complete")
            return
        # broadcast
        ok, txid = self.app.wallet.sendtx(tx)
        self.app.show_info(txid)


class ReceiveScreen(CScreen):

    kvname = 'receive'

    def update(self):
        if not self.screen.address:
            self.get_new_address()

    def get_new_address(self):
        addr = self.app.wallet.get_unused_address(None)
        if addr is None:
            return False
        self.screen.address = addr
        self.screen.amount = ''
        self.screen.message = ''
        return True

    def on_address(self, addr):
        req = self.app.wallet.receive_requests.get(addr)
        if req:
            self.screen.message = unicode(req.get('memo', ''))
            amount = req.get('amount')
            if amount:
                self.screen.amount = self.app.format_amount_and_units(amount)
        Clock.schedule_once(lambda dt: self.update_qr())

    def amount_callback(self, popup):
        amount_label = self.screen.ids.get('amount')
        amount_label.text = popup.ids.amount_label.text
        self.update_qr()

    def get_URI(self):
        from electrum_ltc.util import create_URI
        amount = self.screen.amount
        if amount:
            a, u = self.screen.amount.split()
            assert u == self.app.base_unit
            amount = Decimal(a) * pow(10, self.app.decimal_point())
        return create_URI(self.screen.address, amount, self.screen.message)

    @profiler
    def update_qr(self):
        uri = self.get_URI()
        qr = self.screen.ids.qr
        qr.set_data(uri)

    def do_share(self):
        if platform != 'android':
            return
        uri = self.get_URI()
        from jnius import autoclass, cast
        JS = autoclass('java.lang.String')
        Intent = autoclass('android.content.Intent')
        sendIntent = Intent()
        sendIntent.setAction(Intent.ACTION_SEND)
        sendIntent.setType("text/plain")
        sendIntent.putExtra(Intent.EXTRA_TEXT, JS(uri))
        PythonActivity = autoclass('org.renpy.android.PythonActivity')
        currentActivity = cast('android.app.Activity', PythonActivity.mActivity)
        it = Intent.createChooser(sendIntent, cast('java.lang.CharSequence', JS("Share Litecoin Request")))
        currentActivity.startActivity(it)

    def do_copy(self):
        uri = self.get_URI()
        self.app._clipboard.copy(uri)
        self.app.show_info(_('Request copied to clipboard'))

    def on_amount_or_message(self):
        addr = str(self.screen.address)
        amount = str(self.screen.amount)
        message = str(self.screen.message) #.ids.message_input.text)
        amount = self.app.get_amount(amount) if amount else 0
        req = self.app.wallet.make_payment_request(addr, amount, message, None)
        self.app.wallet.add_payment_request(req, self.app.electrum_config)
        self.app.update_tab('requests')
        Clock.schedule_once(lambda dt: self.update_qr())

    def do_new(self):
        if not self.get_new_address():
            self.app.show_info(_('Please use the existing requests first.'))



pr_text = {
    PR_UNPAID:_('Pending'),
    PR_UNKNOWN:_('Unknown'),
    PR_PAID:_('Paid'),
    PR_EXPIRED:_('Expired')
}
pr_icon = {
    PR_UNPAID: 'atlas://gui/kivy/theming/light/important',
    PR_UNKNOWN: 'atlas://gui/kivy/theming/light/important',
    PR_PAID: 'atlas://gui/kivy/theming/light/confirmed',
    PR_EXPIRED: 'atlas://gui/kivy/theming/light/close'
}


class InvoicesScreen(CScreen):
    kvname = 'invoices'

    def update(self):
        self.menu_actions = [('Pay', self.do_pay), ('Details', self.do_view), ('Delete', self.do_delete)]
        invoices_list = self.screen.ids.invoices_container
        invoices_list.clear_widgets()

        _list = self.app.invoices.sorted_list()
        for pr in _list:
            ci = Factory.InvoiceItem()
            ci.key = pr.get_id()
            ci.requestor = pr.get_requestor()
            ci.memo = pr.get_memo()
            amount = pr.get_amount()
            if amount:
                ci.amount = self.app.format_amount_and_units(amount)
                status = self.app.invoices.get_status(ci.key)
                ci.status = pr_text[status]
                ci.icon = pr_icon[status]
            else:
                ci.amount = _('No Amount')
                ci.status = ''
            exp = pr.get_expiration_date()
            ci.date = format_time(exp) if exp else _('Never')
            ci.screen = self
            invoices_list.add_widget(ci)

        if not _list:
            msg = _('This screen shows the list of payment requests that have been sent to you. You may also use it to store contact addresses.')
            invoices_list.add_widget(EmptyLabel(text=msg))


    def do_pay(self, obj):
        self.app.do_pay(obj)

    def do_view(self, obj):
        pr = self.app.invoices.get(obj.key)
        pr.verify({})
        exp = pr.get_expiration_date()
        popup = Builder.load_file('gui/kivy/uix/ui_screens/invoice.kv')
        popup.ids.requestor_label.text = _("Requestor") + ': ' + pr.get_requestor()
        popup.ids.expiration_label.text = _('Expires') + ': ' + (format_time(exp) if exp else _('Never'))
        popup.ids.memo_label.text = _("Description") + ': ' + pr.get_memo()
        popup.ids.signature_label.text = _("Signature") + ': ' + pr.get_verify_status()
        if pr.tx:
            popup.ids.txid_label.text = _("Transaction ID") + ':\n' + ' '.join(map(''.join, zip(*[iter(pr.tx)]*4)))

        popup.open()

    def do_delete(self, obj):
        from dialogs.question import Question
        def cb():
            self.app.invoices.remove(obj.key)
            self.app.update_tab('invoices')
        d = Question(_('Delete invoice?'), cb)
        d.open()


class RequestsScreen(CScreen):
    kvname = 'requests'

    def update(self):

        self.menu_actions = [('View/Edit', self.do_show), ('Delete', self.do_delete)]
        requests_list = self.screen.ids.requests_container
        requests_list.clear_widgets()
        _list = self.app.wallet.get_sorted_requests(self.app.electrum_config)
        for req in _list:
            address = req['address']
            timestamp = req.get('time', 0)
            amount = req.get('amount')
            expiration = req.get('exp', None)
            status = req.get('status')
            signature = req.get('sig')

            ci = Factory.RequestItem()
            ci.address = address
            ci.memo = self.app.wallet.get_label(address)
            if amount:
                status = req.get('status')
                ci.status = pr_text[status]
            else:
                received = self.app.wallet.get_addr_received(address)
                ci.status = self.app.format_amount_and_units(amount)

            ci.icon = pr_icon[status]
            ci.amount = self.app.format_amount_and_units(amount) if amount else _('No Amount')
            ci.date = format_time(timestamp)
            ci.screen = self
            requests_list.add_widget(ci)

        if not _list:
            msg = _('This screen shows the list of payment requests you made.')
            requests_list.add_widget(EmptyLabel(text=msg))

    def do_show(self, obj):
        self.app.show_request(obj.address)

    def do_delete(self, obj):
        from dialogs.question import Question
        def cb():
            self.app.wallet.remove_payment_request(obj.address, self.app.electrum_config)
            self.update()
        d = Question(_('Delete request?'), cb)
        d.open()




class TabbedCarousel(Factory.TabbedPanel):
    '''Custom TabbedPanel using a carousel used in the Main Screen
    '''

    carousel = ObjectProperty(None)

    def animate_tab_to_center(self, value):
        scrlv = self._tab_strip.parent
        if not scrlv:
            return
        idx = self.tab_list.index(value)
        n = len(self.tab_list)
        if idx in [0, 1]:
            scroll_x = 1
        elif idx in [n-1, n-2]:
            scroll_x = 0
        else:
            scroll_x = 1. * (n - idx - 1) / (n - 1)
        mation = Factory.Animation(scroll_x=scroll_x, d=.25)
        mation.cancel_all(scrlv)
        mation.start(scrlv)

    def on_current_tab(self, instance, value):
        self.animate_tab_to_center(value)

    def on_index(self, instance, value):
        current_slide = instance.current_slide
        if not hasattr(current_slide, 'tab'):
            return
        tab = current_slide.tab
        ct = self.current_tab
        try:
            if ct.text != tab.text:
                carousel = self.carousel
                carousel.slides[ct.slide].dispatch('on_leave')
                self.switch_to(tab)
                carousel.slides[tab.slide].dispatch('on_enter')
        except AttributeError:
            current_slide.dispatch('on_enter')

    def switch_to(self, header):
        # we have to replace the functionality of the original switch_to
        if not header:
            return
        if not hasattr(header, 'slide'):
            header.content = self.carousel
            super(TabbedCarousel, self).switch_to(header)
            try:
                tab = self.tab_list[-1]
            except IndexError:
                return
            self._current_tab = tab
            tab.state = 'down'
            return

        carousel = self.carousel
        self.current_tab.state = "normal"
        header.state = 'down'
        self._current_tab = header
        # set the carousel to load  the appropriate slide
        # saved in the screen attribute of the tab head
        slide = carousel.slides[header.slide]
        if carousel.current_slide != slide:
            carousel.current_slide.dispatch('on_leave')
            carousel.load_slide(slide)
            slide.dispatch('on_enter')

    def add_widget(self, widget, index=0):
        if isinstance(widget, Factory.CScreen):
            self.carousel.add_widget(widget)
            return
        super(TabbedCarousel, self).add_widget(widget, index=index)
