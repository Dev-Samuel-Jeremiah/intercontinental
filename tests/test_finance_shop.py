"""
The school shop and canteen till: selling from stock, paying from the family
wallet or in cash, the parent's spending limits, voids, and how the money
reaches the statement and the accountant's export.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone

from finance.models import (Purchase, Sale, SchoolItem, SpendingLimit, Transaction, Wallet)
from finance.services import export, shop
from finance.services.accounts import statement
from tenants.middleware import set_current_tenant
from users.models import User

from .factories import TenantTestCase, make_admin, make_school, make_student


class ShopBase(TenantTestCase):

    def setUp(self):
        super().setUp()
        self.school = make_school('alpha')
        set_current_tenant(self.school)
        self.admin = make_admin(self.school)
        set_current_tenant(self.school)
        self.blazer = SchoolItem.objects.create(tenant=self.school, name='Blazer (M)',
                                                item_type='UNIFORM', price=Decimal('12000'),
                                                cost_price=Decimal('8500'), stock_qty=10)
        self.jollof = SchoolItem.objects.create(tenant=self.school, name='Jollof rice',
                                                item_type='FOOD', price=Decimal('800'),
                                                cost_price=Decimal('500'), stock_qty=50)
        self.drink = SchoolItem.objects.create(tenant=self.school, name='Zobo drink',
                                               item_type='FOOD', price=Decimal('300'),
                                               stock_qty=50)
        # Two siblings who share the oldest one's wallet.
        self.ada = make_student(self.school, 'STU001', full_name='Ada Bello',
                                parent_email='bello@example.com')
        self.tobi = make_student(self.school, 'STU002', full_name='Tobi Bello',
                                 parent_email='bello@example.com')
        self.wallet = Wallet.objects.create(tenant=self.school, student=self.ada,
                                            balance=Decimal('20000'))

    def _sell(self, basket, **kwargs):
        kwargs.setdefault('user', self.admin)
        return shop.sell(self.school, shop.parse_basket(self.school, basket), **kwargs)

    def _refresh(self, *objects):
        for obj in objects:
            obj.refresh_from_db()


class SellingTests(ShopBase):

    def test_a_cash_sale(self):
        sale = self._sell([(self.blazer.id, 2)], method=Sale.CASH, tendered=25000)
        self._refresh(self.blazer)
        self.assertEqual(sale.total, Decimal('24000.00'))
        self.assertEqual(sale.change_due, Decimal('1000.00'))
        self.assertEqual(self.blazer.stock_qty, 8)
        self.assertEqual(sale.lines.get().unit_price, Decimal('12000'))

    def test_a_receipt_number_is_never_reused(self):
        first = self._sell([(self.blazer.id, 1)])
        second = self._sell([(self.blazer.id, 1)])
        Sale.objects.filter(pk=first.pk).delete()          # a gap in the sequence
        third = self._sell([(self.blazer.id, 1)])
        self.assertNotIn(third.number, (first.number, second.number))
        self.assertTrue(third.number.endswith('-0003'))

    def test_receipt_numbers_follow_each_other(self):
        first = self._sell([(self.blazer.id, 1)])
        second = self._sell([(self.blazer.id, 1)])
        canteen = self._sell([(self.jollof.id, 1)], outlet=Sale.CANTEEN)
        today = timezone.localdate().strftime('%y%m%d')
        self.assertEqual(first.number, f'S{today}-0001')
        self.assertEqual(second.number, f'S{today}-0002')
        self.assertEqual(canteen.number, f'C{today}-0001')

    def test_repeats_in_a_basket_are_merged(self):
        sale = self._sell([(self.jollof.id, 1), (self.jollof.id, 2), (self.drink.id, 1)],
                          outlet=Sale.CANTEEN)
        self.assertEqual(sale.lines.count(), 2)
        self.assertEqual(sale.total, Decimal('2700.00'))

    def test_you_cannot_sell_more_than_is_in_stock(self):
        with self.assertRaisesMessage(shop.SaleError, 'Only 10 Blazer (M) left'):
            self._sell([(self.blazer.id, 11)])
        self._refresh(self.blazer)
        self.assertEqual(self.blazer.stock_qty, 10)
        self.assertFalse(Sale.objects.exists())

    def test_bad_baskets_are_refused(self):
        for basket, message in (([], 'empty'), ([(self.blazer.id, 0)], 'at least 1'),
                                ([(self.blazer.id, 'two')], 'whole number'),
                                ([(999999, 1)], 'no longer on sale')):
            with self.assertRaisesMessage(shop.SaleError, message):
                shop.parse_basket(self.school, basket)

    def test_an_item_taken_off_sale_cannot_be_sold(self):
        self.blazer.is_active = False
        self.blazer.save()
        with self.assertRaises(shop.SaleError):
            self._sell([(self.blazer.id, 1)])

    def test_cash_handed_over_must_cover_the_total(self):
        with self.assertRaisesMessage(shop.SaleError, 'less than the total'):
            self._sell([(self.blazer.id, 1)], method=Sale.CASH, tendered=10000)

    def test_cost_is_kept_for_profit(self):
        sale = self._sell([(self.blazer.id, 2)])
        self.assertEqual(sale.cost, Decimal('17000.00'))

    def test_another_schools_items_cannot_be_sold(self):
        other = make_school('beta')
        set_current_tenant(other)
        theirs = SchoolItem.objects.create(tenant=other, name='Their tie', item_type='UNIFORM',
                                           price=Decimal('1000'), stock_qty=5)
        set_current_tenant(None)
        with self.assertRaises(shop.SaleError):
            shop.parse_basket(self.school, [(theirs.id, 1)])


class WalletSaleTests(ShopBase):

    def test_the_family_wallet_pays(self):
        sale = self._sell([(self.jollof.id, 2)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                          student=self.tobi)
        self._refresh(self.wallet)
        self.assertEqual(self.wallet.balance, Decimal('18400.00'))   # Tobi spends Ada's wallet
        self.assertEqual(sale.transaction.category, 'CANTEEN')
        self.assertIn('Tobi Bello', sale.transaction.description)

    def test_the_parent_sees_what_was_bought(self):
        self._sell([(self.blazer.id, 1)], method=Sale.WALLET, student=self.ada)
        purchase = Purchase.objects.get(student=self.ada)
        self.assertEqual((purchase.item, purchase.total_price), (self.blazer, Decimal('12000.00')))

    def test_a_wallet_sale_needs_a_student(self):
        with self.assertRaisesMessage(shop.SaleError, 'Choose the student'):
            self._sell([(self.blazer.id, 1)], method=Sale.WALLET)

    def test_not_enough_in_the_wallet(self):
        with self.assertRaisesMessage(shop.SaleError, 'The family wallet has NGN 20,000.00'):
            self._sell([(self.blazer.id, 2)], method=Sale.WALLET, student=self.ada)
        self._refresh(self.wallet, self.blazer)
        self.assertEqual(self.wallet.balance, Decimal('20000'))
        self.assertEqual(self.blazer.stock_qty, 10)

    def test_a_student_with_no_wallet_is_told_it_is_empty(self):
        loner = make_student(self.school, 'STU009', full_name='New Pupil')
        with self.assertRaisesMessage(shop.SaleError, 'The family wallet has NGN 0.00'):
            self._sell([(self.drink.id, 1)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                       student=loner)


class SpendingLimitTests(ShopBase):

    def test_the_daily_canteen_limit(self):
        SpendingLimit.objects.create(tenant=self.school, student=self.tobi,
                                     daily_canteen=Decimal('1500'))
        self._sell([(self.jollof.id, 1)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                   student=self.tobi)
        with self.assertRaisesMessage(shop.SaleError, 'can spend NGN 700.00 more'):
            self._sell([(self.jollof.id, 1)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                       student=self.tobi)
        self._sell([(self.drink.id, 2)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                   student=self.tobi)                               # 600 fits

    def test_each_child_has_their_own_limit(self):
        SpendingLimit.objects.create(tenant=self.school, student=self.tobi,
                                     daily_canteen=Decimal('500'))
        self._sell([(self.jollof.id, 2)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                   student=self.ada)                                # Ada has no limit

    def test_yesterdays_spending_does_not_count(self):
        SpendingLimit.objects.create(tenant=self.school, student=self.tobi,
                                     daily_canteen=Decimal('1000'))
        sale = self._sell([(self.jollof.id, 1)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                          student=self.tobi)
        Sale.objects.filter(pk=sale.pk).update(created_at=timezone.now() - timedelta(days=1))
        self._sell([(self.jollof.id, 1)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                   student=self.tobi)

    def test_paying_cash_is_not_limited(self):
        SpendingLimit.objects.create(tenant=self.school, student=self.tobi,
                                     daily_canteen=Decimal('0'), shop_allowed=False)
        self._sell([(self.jollof.id, 3)], outlet=Sale.CANTEEN, method=Sale.CASH,
                   student=self.tobi)
        self._sell([(self.blazer.id, 1)], method=Sale.CASH, student=self.tobi)

    def test_a_parent_can_stop_shop_purchases_on_the_wallet(self):
        SpendingLimit.objects.create(tenant=self.school, student=self.tobi, shop_allowed=False)
        with self.assertRaisesMessage(shop.SaleError, 'has not allowed shop purchases'):
            self._sell([(self.blazer.id, 1)], method=Sale.WALLET, student=self.tobi)

    def test_a_parent_buying_is_not_bound_by_their_childs_limit(self):
        SpendingLimit.objects.create(tenant=self.school, student=self.tobi, shop_allowed=False)
        self._sell([(self.blazer.id, 1)], method=Sale.WALLET, student=self.tobi,
                   enforce_limits=False)


class VoidTests(ShopBase):

    def test_voiding_a_wallet_sale_puts_everything_back(self):
        sale = self._sell([(self.blazer.id, 1)], method=Sale.WALLET, student=self.ada)
        shop.void(sale, self.admin, 'Wrong size')
        self._refresh(sale, self.wallet, self.blazer)
        self.assertEqual(sale.status, Sale.VOIDED)
        self.assertEqual(self.wallet.balance, Decimal('20000.00'))
        self.assertEqual(self.blazer.stock_qty, 10)
        self.assertEqual(Purchase.objects.get().status, 'REFUNDED')
        self.assertTrue(Transaction.objects.filter(category='REVERSAL').exists())

    def test_a_sale_cannot_be_voided_twice(self):
        sale = self._sell([(self.blazer.id, 1)])
        shop.void(sale, self.admin, 'Mistake')
        with self.assertRaisesMessage(shop.SaleError, 'already voided'):
            shop.void(sale, self.admin, 'Again')

    def test_a_void_needs_a_reason(self):
        sale = self._sell([(self.blazer.id, 1)])
        with self.assertRaisesMessage(shop.SaleError, 'Say why'):
            shop.void(sale, self.admin, '  ')

    def test_a_voided_canteen_sale_frees_up_the_days_limit(self):
        SpendingLimit.objects.create(tenant=self.school, student=self.tobi,
                                     daily_canteen=Decimal('800'))
        sale = self._sell([(self.jollof.id, 1)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                          student=self.tobi)
        shop.void(sale, self.admin, 'Rang up twice')
        self._sell([(self.jollof.id, 1)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                   student=self.tobi)


class MoneyTests(ShopBase):

    def test_the_report(self):
        self._sell([(self.blazer.id, 1)], method=Sale.CASH)
        self._sell([(self.jollof.id, 2)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                   student=self.ada)
        voided = self._sell([(self.drink.id, 1)], outlet=Sale.CANTEEN)
        shop.void(voided, self.admin, 'Mistake')
        report = shop.summary(self.school)
        self.assertEqual(report['count'], 2)
        self.assertEqual(report['total'], Decimal('13600.00'))
        self.assertEqual(report['takings'], Decimal('12000.00'))
        self.assertEqual(report['from_wallets'], Decimal('1600.00'))
        self.assertEqual(report['profit'], Decimal('4100.00'))    # 3,500 + 600
        self.assertEqual(report['voided'], 1)
        self.assertEqual(report['best_sellers'][0]['name'], 'Jollof rice')

    def test_low_stock(self):
        self.blazer.stock_qty = 3
        self.blazer.save()
        self.assertEqual(list(shop.low_stock(self.school)), [self.blazer])

    def test_cash_takings_are_income_wallet_sales_are_not(self):
        self._sell([(self.blazer.id, 1)], method=Sale.POS)
        self._sell([(self.jollof.id, 2)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                   student=self.ada)
        report = statement(self.school)
        self.assertEqual(report['shop_takings'], Decimal('12000.00'))
        self.assertEqual(report['income'], Decimal('12000.00'))

    def test_the_export_posts_till_sales(self):
        self._sell([(self.blazer.id, 1)], method=Sale.CASH)
        self._sell([(self.jollof.id, 1)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                   student=self.ada)
        today = timezone.localdate()
        entries = export.journal(self.school, today, today)
        self.assertEqual(len(entries), 1)
        lines = {(l['code'], 'Dr' if l['debit'] else 'Cr'): l['debit'] or l['credit']
                 for l in entries[0]['lines']}
        self.assertEqual(lines, {('1010', 'Dr'): Decimal('12000.00'),
                                 ('4200', 'Cr'): Decimal('12000.00')})


@override_settings(ROOT_DOMAIN='localhost', ALLOWED_HOSTS=['*'])
class TillPageTests(ShopBase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def _post(self, path, data, **kwargs):
        return self.client.post(path, data, HTTP_HOST=self.host(self.school), **kwargs)

    def _get(self, path):
        return self.client.get(path, HTTP_HOST=self.host(self.school))

    def test_the_till_shows_the_right_items(self):
        shop_page = self._get('/finance/admin/till/')
        canteen = self._get('/finance/admin/till/?outlet=CANTEEN')
        self.assertEqual(list(shop_page.context['items']), [self.blazer])
        self.assertEqual(set(canteen.context['items']), {self.jollof, self.drink})

    def test_charging_from_the_till(self):
        response = self._post('/finance/admin/till/', {
            'outlet': 'CANTEEN', 'method': 'WALLET', 'student_id': self.tobi.id,
            f'item_{self.jollof.id}': '2', f'item_{self.drink.id}': '1'})
        sale = Sale.objects.get()
        self.assertRedirects(response, f'/finance/admin/sales/{sale.id}/',
                             fetch_redirect_response=False)
        self.assertEqual(sale.total, Decimal('1900.00'))
        self.assertEqual(sale.sold_by, self.admin)

    def test_a_refused_sale_shows_why(self):
        response = self._post('/finance/admin/till/', {
            'outlet': 'SHOP', 'method': 'CASH', f'item_{self.blazer.id}': '99'}, follow=True)
        self.assertContains(response, 'Only 10 Blazer (M) left')

    def test_the_receipt(self):
        sale = self._sell([(self.blazer.id, 1)], method=Sale.WALLET, student=self.ada)
        response = self._get(f'/finance/admin/sales/{sale.id}/')
        self.assertContains(response, sale.number)
        self.assertContains(response, 'Wallet balance now')

    def test_finding_a_student_at_the_till(self):
        SpendingLimit.objects.create(tenant=self.school, student=self.tobi,
                                     daily_canteen=Decimal('1000'))
        data = self._get('/finance/admin/till/students/?q=tobi&outlet=CANTEEN').json()
        hit = data['results'][0]
        self.assertEqual((hit['name'], hit['balance'], hit['canteen_left']),
                         ('Tobi Bello', '20000.00', '1000.00'))

    def test_voiding_from_the_sales_page(self):
        sale = self._sell([(self.blazer.id, 1)])
        self._post('/finance/admin/sales/', {'action': 'void', 'sale_id': sale.id,
                                             'reason': 'Customer changed mind'})
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.VOIDED)

    def test_the_sales_report_page(self):
        self._sell([(self.blazer.id, 1)])
        response = self._get('/finance/admin/sales/')
        self.assertEqual(response.context['summary']['count'], 1)

    def test_a_parent_sets_a_limit(self):
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        self._post('/finance/wallet/limits/', {'child_id': self.tobi.id,
                                               'daily_canteen': '1200'})
        limit = SpendingLimit.objects.get(student=self.tobi)
        self.assertEqual(limit.daily_canteen, Decimal('1200'))
        self.assertFalse(limit.shop_allowed)          # box left unticked
        self.assertEqual(limit.set_by, parent)

    def test_the_parent_wallet_page_shows_each_childs_limits(self):
        SpendingLimit.objects.create(tenant=self.school, student=self.tobi,
                                     daily_canteen=Decimal('1500'))
        self._sell([(self.jollof.id, 1)], outlet=Sale.CANTEEN, method=Sale.WALLET,
                   student=self.tobi)
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        response = self._get('/finance/wallet/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Spending at School')
        self.assertContains(response, 'Spent at the canteen today: ₦800.00 of ₦1500.00')
        self.assertEqual(len(response.context['spending']), 2)

    def test_a_parent_cannot_set_a_limit_for_someone_elses_child(self):
        stranger = make_student(self.school, 'STU050', parent_email='other@example.com')
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        self._post('/finance/wallet/limits/', {'child_id': stranger.id, 'daily_canteen': '0'})
        self.assertFalse(SpendingLimit.objects.exists())

    def test_a_parent_buying_through_the_wallet_page(self):
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        response = self._post('/finance/wallet/buy/', {'item_id': self.blazer.id,
                                                       'quantity': '1',
                                                       'child_id': self.tobi.id})
        sale = Sale.objects.get()
        self.assertRedirects(response, f'/finance/receipt/{sale.transaction_id}/',
                             fetch_redirect_response=False)
        self._refresh(self.wallet, self.blazer)
        self.assertEqual(self.wallet.balance, Decimal('8000.00'))
        self.assertEqual(self.blazer.stock_qty, 9)

    def test_a_silly_quantity_on_the_wallet_page_does_not_crash(self):
        parent = User.objects.create_user(username=f'parent_{self.ada.id}', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        response = self._post('/finance/wallet/buy/', {'item_id': self.blazer.id,
                                                       'quantity': 'lots'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Sale.objects.exists())

    def test_a_parent_cannot_use_the_till(self):
        parent = User.objects.create_user(username='par9', password='x',
                                          tenant=self.school, is_parent=True)
        self.client.force_login(parent)
        for path in ('/finance/admin/till/', '/finance/admin/sales/',
                     '/finance/admin/till/students/?q=ada'):
            self.assertNotEqual(self._get(path).status_code, 200, path)
