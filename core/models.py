from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class Branch(models.Model):
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=20, unique=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "branch"
        verbose_name_plural = "branches"
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class Warehouse(models.Model):
    class WarehouseType(models.TextChoices):
        MASTER = "MASTER", "Master Warehouse"
        BRANCH = "BRANCH", "Branch Warehouse"

    name = models.CharField(max_length=150)
    code = models.CharField(max_length=30, unique=True)
    warehouse_type = models.CharField(
        max_length=10,
        choices=WarehouseType.choices,
    )
    branch = models.OneToOneField(
        Branch,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="warehouse",
    )
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "warehouse"
        verbose_name_plural = "warehouses"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["warehouse_type"],
                condition=Q(warehouse_type="MASTER"),
                name="one_master_warehouse_only",
            ),
            models.CheckConstraint(
                condition=(
                    Q(warehouse_type="MASTER", branch__isnull=True)
                    | Q(warehouse_type="BRANCH", branch__isnull=False)
                ),
                name="warehouse_type_matches_branch",
            ),
        ]

    def clean(self):
        super().clean()

        if self.warehouse_type == self.WarehouseType.MASTER and self.branch_id:
            raise ValidationError(
                {"branch": "The master warehouse cannot belong to a branch."}
            )

        if self.warehouse_type == self.WarehouseType.BRANCH and not self.branch_id:
            raise ValidationError(
                {"branch": "A branch warehouse must be assigned to a branch."}
            )

    def __str__(self):
        return f"{self.name} ({self.get_warehouse_type_display()})"
