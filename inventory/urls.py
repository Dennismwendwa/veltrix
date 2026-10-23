from django.urls import path

from inventory import views

app_name = "inventory"

urlpatterns = [
    path("replenishment/", views.replenishment_list,
        name="replenishment-list",
    ),
]