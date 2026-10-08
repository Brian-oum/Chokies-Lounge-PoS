from django.urls import path

from . import views as v

urlpatterns = [
    # session
    path("auth/pin-login/", v.PinLoginView.as_view()),
    path("auth/logout/", v.LogoutView.as_view()),
    path("auth/me/", v.MeView.as_view()),
    path("auth/roster/", v.RosterView.as_view()),

    # shifts: manager creates / manages
    path("shifts/", v.ShiftListCreateView.as_view()),
    path("shifts/<int:pk>/", v.ShiftDetailView.as_view()),
    path("shifts/<int:pk>/staff/", v.ShiftStaffAddView.as_view()),
    path("shifts/<int:pk>/staff/<int:user_id>/end/", v.ShiftStaffEndView.as_view()),
    path("shifts/<int:pk>/close/", v.ShiftCloseView.as_view()),
    # shifts: the logged-in waiter / cashier
    path("shift/current/", v.MyShiftView.as_view()),
    path("shift/cash/", v.MyDrawerCashView.as_view()),
    path("shift/end/", v.MyShiftEndView.as_view()),
    path("shift/sales/", v.MySalesView.as_view()),          # the signed-in waiter's own paid bills ("My sales")

    # menu, tables, orders
    path("menu/", v.MenuView.as_view()),
    path("tables/", v.TableListView.as_view()),
    path("orders/", v.OrderCreateView.as_view()),
    path("orders/<int:pk>/", v.OrderDetailView.as_view()),
    path("orders/<int:pk>/page/", v.OrderPageView.as_view()),               # the full-page order / invoice view
    path("orders/<int:pk>/items/", v.AddItemView.as_view()),
    path("orders/<int:pk>/items/bulk/", v.BulkAddItemsView.as_view()),   # used by the order form's "Add items"
    path("orders/<int:pk>/items/<int:item_id>/", v.ItemQuantityView.as_view()),
    path("orders/<int:pk>/items/<int:item_id>/void/", v.VoidItemView.as_view()),
    path("orders/<int:pk>/send/", v.SendOrderView.as_view()),
    path("orders/<int:pk>/cancel/", v.CancelOrderView.as_view()),
    path("orders/<int:pk>/bill/", v.BillOrderView.as_view()),
    path("orders/<int:pk>/merge/", v.MergeOrdersView.as_view()),
    path("orders/<int:pk>/split/", v.SplitOrderView.as_view()),

    # billing
    path("bills/", v.BillListView.as_view()),
    path("bills/<int:pk>/", v.BillDetailView.as_view()),
    path("bills/<int:pk>/receipt/", v.BillReceiptView.as_view()),
    path("bills/<int:pk>/cancel/", v.CancelBillView.as_view()),
    path("bills/<int:pk>/pay/", v.PayView.as_view()),
    path("payments/config/", v.PaymentConfigView.as_view()),                       # till number, bank details, is STK set up
    path("payments/mpesa/stk/", v.MpesaStkView.as_view()),                         # send the STK push
    path("payments/mpesa/stk/<str:checkout_id>/", v.MpesaStkStatusView.as_view()),  # poll its result
    path("payments/mpesa/callback/", v.MpesaCallbackView.as_view()),               # Safaricom posts the result here
    path("payments/<int:pk>/refund/", v.RefundView.as_view()),

    # end of day
    path("reports/z/preview/", v.ZPreviewView.as_view()),
    path("reports/z/close/", v.ZCloseView.as_view()),
]