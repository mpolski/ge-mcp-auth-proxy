"""OpenTelemetry distributed tracing setup for Google Cloud Trace integration."""

import logging
import warnings
from typing import Optional
from fastapi import FastAPI

from opentelemetry import trace
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.cloud_trace_propagator import CloudTraceFormatPropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from app.config import settings

logger = logging.getLogger(__name__)

_provider: Optional[TracerProvider] = None
_initialized = False


def setup_telemetry(app: FastAPI, custom_exporter: Optional[SpanExporter] = None) -> TracerProvider:
    """Initialize OpenTelemetry tracing with Google Cloud Trace exporter and context propagation."""
    global _provider, _initialized

    if _provider is None:
        # Always configure propagators so X-Cloud-Trace-Context and W3C traceparent headers are extracted/injected
        set_global_textmap(
            CompositePropagator(
                [
                    CloudTraceFormatPropagator(),
                    TraceContextTextMapPropagator(),
                ]
            )
        )

        import os

        # K_SERVICE is injected by Cloud Run. The fallback is only used off-platform
        # (local runs, tests), so it is derived from the configured vendor rather than
        # naming any one deployment.
        service_name = os.environ.get(
            "K_SERVICE", f"ge-{settings.UPSTREAM_SERVICE_NAME}-proxy"
        )
        project_number = settings.GCP_PROJECT_NUMBER or settings.GCP_PROJECT_ID or "unknown"
        mcp_server_urn = os.environ.get(
            "MCP_SERVER_URN",
            f"urn:mcp:projects-{project_number}:projects:{project_number}"
            f":locations:{settings.GCP_REGION}:run:services:{service_name}",
        )
        resource_attributes = {
            "service.name": service_name,
            "service.version": "1.0.0",
            "cloud.provider": "gcp",
            "gcp.mcp.server.id": mcp_server_urn,
        }
        if settings.GCP_PROJECT_ID:
            resource_attributes["gcp.project_id"] = settings.GCP_PROJECT_ID
        if settings.GCP_PROJECT_NUMBER:
            resource_attributes["cloud.account.id"] = settings.GCP_PROJECT_NUMBER
        resource = Resource.create(resource_attributes)
        _provider = TracerProvider(resource=resource)

        if settings.OTEL_ENABLED:
            # 1. Initialize OTLP gRPC Exporter to Google Cloud Telemetry API (required for Agent Platform Observability)
            try:
                import google.auth
                import google.auth.transport.requests
                import grpc
                from google.auth.transport.grpc import AuthMetadataPlugin
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

                credentials, _ = google.auth.default()
                request = google.auth.transport.requests.Request()
                auth_metadata_plugin = AuthMetadataPlugin(credentials=credentials, request=request)
                channel_creds = grpc.composite_channel_credentials(
                    grpc.ssl_channel_credentials(),
                    grpc.metadata_call_credentials(auth_metadata_plugin),
                )
                otlp_exporter = OTLPSpanExporter(
                    credentials=channel_creds,
                    endpoint="https://telemetry.googleapis.com:443/v1/traces",
                )
                _provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
                logger.info(
                    "OpenTelemetry Google Cloud Telemetry API OTLP exporter initialized (urn=%s)",
                    mcp_server_urn,
                )
            except Exception as e:
                logger.warning("Could not initialize Google Cloud Telemetry API OTLP exporter: %s", e)

            # 2. Initialize CloudTraceSpanExporter for direct Cloud Trace v2 API ingestion
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

                    exporter = CloudTraceSpanExporter(project_id=settings.GCP_PROJECT_ID)
                    _provider.add_span_processor(BatchSpanProcessor(exporter))
                logger.info(
                    "OpenTelemetry Cloud Trace exporter initialized (project_id=%s)",
                    settings.GCP_PROJECT_ID,
                )
            except Exception as e:
                logger.warning(
                    "Could not initialize CloudTraceSpanExporter (tracing will run in-process without GCP export): %s",
                    e,
                )
        else:
            logger.info("OpenTelemetry tracing disabled (OTEL_ENABLED=False)")

        trace.set_tracer_provider(_provider)

    if custom_exporter is not None:
        _provider.add_span_processor(SimpleSpanProcessor(custom_exporter))

    if not _initialized:
        # Instrument FastAPI ingress and outbound HTTPX calls once per process
        FastAPIInstrumentor.instrument_app(app, tracer_provider=_provider)
        HTTPXClientInstrumentor().instrument(tracer_provider=_provider)
        _initialized = True

    return _provider


def get_tracer(name: str = "mcp_identity_broker") -> trace.Tracer:
    """Return an OpenTelemetry Tracer instance."""
    return trace.get_tracer(name)
