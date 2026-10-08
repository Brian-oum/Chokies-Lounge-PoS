"""Manager back-office pages. In the project urls.py:  path("manager/", include("Chokies.manager_urls"))"""
from django.urls import path

from . import manager_reports as r
from . import manager_views as m

app_name = "manager"

urlpatterns = [
    path("", m.dashboard, name="dashboard"),

    # purchases + LPOs
    path("purchases/", m.purchases, name="purchases"),
    path("purchases/new/", m.purchase_new, name="purchase_new"),
    path("purchases/<int:pk>/", m.purchase_detail, name="purchase_detail"),
    path("lpos/", m.lpos, name="lpos"),
    path("lpos/new/", m.lpo_new, name="lpo_new"),
    path("lpos/<int:pk>/", m.lpo_detail, name="lpo_detail"),
    path("lpos/<int:pk>/<str:action>/", m.lpo_action, name="lpo_action"),          # send | cancel | close
    path("receive/", m.receive, name="receive"),

    # inventory
    path("items/", m.items, name="items"),
    path("items/new/", m.item_new, name="item_new"),
    path("items/<int:pk>/", m.item_detail, name="item_detail"),
    path("items/<int:pk>/edit/", m.item_edit, name="item_edit"),
    path("items/<int:pk>/toggle/", m.item_toggle, name="item_toggle"),
    path("balances/", m.balances, name="balances"),
    path("stock-takes/", m.stock_takes, name="stock_takes"),
    path("stock-takes/<int:pk>/", m.stock_take_detail, name="stock_take"),
    path("issue/", m.issue, name="issue"),

    # voids + discounts
    path("voids/", m.voids, name="voids"),
    path("voids/item/<int:item_id>/", m.void_item_action, name="void_item"),
    path("voids/bill/<int:pk>/cancel/", m.cancel_bill_action, name="cancel_bill"),
    path("discounts/", m.discounts, name="discounts"),
    path("discounts/<int:pk>/<str:action>/", m.discount_action, name="discount_action"),   # apply | remove

    path("recipes/", m.recipes, name="recipes"),
    path("recipes/<int:pk>/", m.recipe_edit, name="recipe_edit"),

    # reports (managers / admins only)
    path("reports/", r.index, name="reports"),
    path("reports/<slug:slug>/", r.report, name="report"),
]