from django.db import models


class SyncRun(models.Model):
    """One fetch from Trade Republic. Doubles as the worker's job queue."""

    class Mode(models.TextChoices):
        INCREMENTAL = "incremental"
        FULL = "full"
        DOCUMENTS = "documents"

    class Status(models.TextChoices):
        QUEUED = "queued"
        AWAITING_APPROVAL = "awaiting_approval"  # waiting for the tap in the TR app
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"

    ACTIVE = (Status.QUEUED, Status.AWAITING_APPROVAL, Status.RUNNING)

    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.INCREMENTAL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    stage = models.CharField(max_length=120, blank=True)
    message = models.TextField(blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    counts = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"SyncRun #{self.pk} {self.mode} {self.status}"

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None


class Setting(models.Model):
    """Small key/value store for per-installation preferences."""

    key = models.CharField(max_length=64, primary_key=True)
    value = models.JSONField(null=True)

    @classmethod
    def get(cls, key: str, default=None):
        row = cls.objects.filter(key=key).first()
        return default if row is None else row.value

    @classmethod
    def put(cls, key: str, value) -> None:
        cls.objects.update_or_create(key=key, defaults={"value": value})
