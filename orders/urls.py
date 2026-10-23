from django.urls import path

from orders import views

app_name = "orders"

urlpatterns = [
    path("", views.order_list, name="order_list"),
    path("<int:pk>/", views.order_detail, name="order_detail"),
    path("<int:pk>/ready-for-collection/",
         views.mark_ready_for_collection,
        name="mark_ready_for_collection",
    ),
    path("<int:pk>/dispatch/", views.dispatch_for_delivery,
        name="dispatch_for_delivery",
    ),
    path("<int:pk>/complete/", views.complete_order,
        name="complete_order",
    ),
]