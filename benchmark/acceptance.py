"""Public contracts executed from outside agent worktrees with frozen baseline fixtures.

These tests intentionally fail on unmodified Ridgeline. Run only selected task
nodes while integrating, then all nodes for the final gate.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import db
from app.models import Customer, GreenCoffeeLot, Order, OrderDueDate, OrderItem, Product, RoastBatch, Shipment, Subscription, TruckingService


@pytest.fixture(autouse=True)
def benchmark_data(app):
    """Extend the committed baseline fixture without relying on local changes."""
    with app.app_context():
        customer = db.session.get(Order, app.config["TEST_IDS"]["own_order"]).customer
        app.config["TEST_IDS"]["customer"] = customer.id
        product = Product.query.first()
        lot = GreenCoffeeLot(lot_code="BENCH-LOT", origin="Colombia", weight_kg_received=100,
                             weight_kg_remaining=80, cost_per_kg_usd=5)
        db.session.add_all([
            lot,
            RoastBatch(batch_number="BENCH-BATCH", lot=lot, product=product, green_weight_kg=20, roasted_weight_kg=17),
            TruckingService(name="Benchmark Truck", service_area="local", capacity_cases=200),
            Subscription(customer=customer, product=product, frequency="monthly", quantity=2, status="active"),
        ])
        db.session.commit()


def payload(client, headers, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    return response.get_json()


def test_task01(app, client, api_headers):
    with app.app_context():
        product = Product.query.first()
        product.cases_in_stock = 3
        product.bags_in_stock = 7
        product.wholesale_price_per_case = Decimal("12.35")
        product.retail_price_per_bag = Decimal("4.21")
        expected = {"cases": 37.05, "bags": 29.47, "total": 66.52}
        assert product.stock_value == expected
        db.session.commit()
        assert payload(client, api_headers, "/api/products")[0]["stock_value"] == expected
        product.cases_in_stock = product.bags_in_stock = 0
        assert product.stock_value == {"cases": 0.0, "bags": 0.0, "total": 0.0}


@pytest.mark.parametrize("cases,bags,active,expected", [(5,10,True,False), (4,10,True,True), (5,9,True,True), (0,0,False,False)])
def test_task02(app, client, api_headers, cases, bags, active, expected):
    with app.app_context():
        product = Product.query.first()
        product.cases_in_stock, product.bags_in_stock, product.active = cases, bags, active
        assert product.needs_restock is expected
        db.session.commit()
        assert payload(client, api_headers, "/api/products")[0]["needs_restock"] is expected


def test_task03(app, client, api_headers):
    with app.app_context():
        lot = GreenCoffeeLot.query.order_by(GreenCoffeeLot.lot_code).first()
        lot.weight_kg_remaining, lot.cost_per_kg_usd = Decimal("12.34"), Decimal("5.67")
        assert lot.remaining_value_usd == 69.97
        db.session.commit()
        assert payload(client, api_headers, "/api/inventory")[0]["remaining_value_usd"] == 69.97
        lot.weight_kg_remaining = Decimal("0")
        assert lot.remaining_value_usd == 0.0


@pytest.mark.parametrize("green,roasted,expected", [(20,17,15.0), (20,0,100.0), (0,0,None), (20,None,None), (3,2,33.3)])
def test_task04(app, client, api_headers, green, roasted, expected):
    with app.app_context():
        batch = RoastBatch.query.first()
        batch.green_weight_kg, batch.roasted_weight_kg = green, roasted
        assert batch.weight_loss_pct == expected
        db.session.commit()
        assert payload(client, api_headers, "/api/roast-batches")[0]["weight_loss_pct"] == expected


@pytest.mark.parametrize("allocated,expected", [(None,2), (1,1), (7,0)])
def test_task05(app, client, api_headers, allocated, expected):
    with app.app_context():
        order = db.session.get(Order, app.config["TEST_IDS"]["own_order"])
        order.shipments[0].cases = 100  # Parcel cases must be ignored.
        order.shipments.append(Shipment(method="delivery_truck", status="planned", load_number=2, cases=allocated))
        value = {"ordered_quantity": 2, "allocated_quantity": allocated or 0, "unallocated_quantity": expected}
        assert order.shipment_coverage == value
        db.session.commit()
        detail = payload(client, api_headers, f"/api/orders/{order.id}")
        assert detail["shipment_coverage"] == value
        rows = payload(client, api_headers, "/api/orders")
        assert next(row for row in rows if row["id"] == order.id)["shipment_coverage"] == value
        order.items.clear()
        order.shipments.clear()
        assert order.shipment_coverage == dict(ordered_quantity=0, allocated_quantity=0, unallocated_quantity=0)


@pytest.mark.parametrize("offset,status,expected", [(-4,"pending",4), (0,"confirmed",0), (3,"roasting",0), (-4,"delivered",0), (-4,"cancelled",0), (None,"pending",0)])
def test_task06(app, client, api_headers, offset, status, expected):
    with app.app_context():
        order = db.session.get(Order, app.config["TEST_IDS"]["own_order"])
        order.status = status
        order.delivery_deadline = None if offset is None else OrderDueDate(due_date=date.today()+timedelta(days=offset))
        assert order.days_overdue == expected
        db.session.commit()
        assert payload(client, api_headers, f"/api/orders/{order.id}")["days_overdue"] == expected
        assert next(row for row in payload(client, api_headers, "/api/orders") if row["id"] == order.id)["days_overdue"] == expected


def test_task07(app, client, api_headers):
    with app.app_context():
        customer = db.session.get(Customer, app.config["TEST_IDS"]["customer"])
        assert customer.delivered_revenue == 0.0
        order = customer.orders[0]
        order.status = "delivered"
        order.items[0].quantity, order.items[0].unit_price = 3, Decimal("0.10")
        product = Product.query.first()
        for status in ("cancelled", "pending", "confirmed", "roasting", "shipped", "delivered"):
            customer.orders.append(Order(order_number=f"REVENUE-{status}", order_type="wholesale", status=status,
                                         items=[OrderItem(product=product, quantity=2, unit_price=Decimal("1.23"))]))
        assert customer.delivered_revenue == 2.76
        db.session.commit()
        assert payload(client, api_headers, f"/api/accounts/{customer.id}")["delivered_revenue"] == 2.76
        assert next(r for r in payload(client, api_headers, "/api/accounts") if r["id"] == customer.id)["delivered_revenue"] == 2.76


def test_task08(app, client, api_headers):
    with app.app_context():
        customer = db.session.get(Customer, app.config["TEST_IDS"]["customer"])
        customer.subscriptions.clear()
        assert customer.active_subscription_quantity == 0
        for i, status in enumerate(("active", "active", "paused", "cancelled")):
            customer.subscriptions.append(Subscription(product=Product.query.first(), quantity=i+2, status=status, frequency="weekly"))
        assert customer.active_subscription_quantity == 5
        db.session.commit()
        assert payload(client, api_headers, f"/api/accounts/{customer.id}")["active_subscription_quantity"] == 5
        assert next(r for r in payload(client, api_headers, "/api/accounts") if r["id"] == customer.id)["active_subscription_quantity"] == 5


@pytest.mark.parametrize("method,cases,capacity,expected", [("delivery_truck",100,200,50.0), ("delivery_truck",250,200,125.0), ("delivery_truck",0,200,0.0), ("delivery_truck",None,200,None), ("ups",50,200,None), ("delivery_truck",10,0,None), ("delivery_truck",10,None,None)])
def test_task09(app, client, api_headers, method, cases, capacity, expected):
    with app.app_context():
        shipment = Shipment.query.first()
        shipment.method, shipment.cases = method, cases
        service = TruckingService.query.first()
        service.capacity_cases = capacity or 0
        shipment.trucking_service = service if capacity is not None else None
        assert shipment.capacity_utilization_pct == expected
        db.session.commit()
        assert next(r for r in payload(client, api_headers, "/api/shipments") if r["id"] == shipment.id)["capacity_utilization_pct"] == expected
        detail = payload(client, api_headers, f"/api/orders/{shipment.order_id}")
        assert next(r for r in detail["shipments"] if r["id"] == shipment.id)["capacity_utilization_pct"] == expected


@pytest.mark.parametrize("frequency,status,quantity,expected", [("weekly","active",2,8.67), ("biweekly","active",2,4.33), ("monthly","active",3,3.0), ("weekly","paused",2,0.0), ("weekly","cancelled",2,0.0), ("unknown","active",2,0.0), ("weekly","active",0,0.0)])
def test_task10(app, client, api_headers, frequency, status, quantity, expected):
    with app.app_context():
        sub = Subscription.query.first()
        sub.frequency, sub.status, sub.quantity = frequency, status, quantity
        assert sub.monthly_quantity == expected
        db.session.commit()
        assert next(r for r in payload(client, api_headers, "/api/subscriptions") if r["id"] == sub.id)["monthly_quantity"] == expected
        detail = payload(client, api_headers, f"/api/accounts/{sub.customer_id}")
        assert next(r for r in detail["subscriptions"] if r["id"] == sub.id)["monthly_quantity"] == expected
