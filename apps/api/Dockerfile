# ── Nguồn engine ALICE CORE ─────────────────────────────────────────────────
# Engine ở repo riêng. Stage này có HAI cách được thoả, và cả hai cho ra cây thư mục
# giống hệt nhau nên `COPY --from=alicecore .` bên dưới không cần biết mình đang ở đường nào:
#
#   - CI  : image do repo alice-core publish (mặc định dưới đây).
#   - Dev : `--build-context alicecore=<thư mục alice-core trên máy>` GHI ĐÈ stage này,
#           compose khai báo qua `additional_contexts` trong compose.dev.yaml.
ARG ALICECORE_IMAGE=ghcr.io/blueberry-sensei/alice-core:latest
FROM ${ALICECORE_IMAGE} AS alicecore

FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Image mặc định chỉ cài phụ thuộc cho SQLite/LanceDB cục bộ; production truyền postgres.
ARG SAG_API_EXTRAS=""

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

# ── ALICE CORE ──────────────────────────────────────────────────────────────
# Engine là package CỤC BỘ, KHÔNG có trên PyPI. Nguồn của stage `alicecore` xem phần đầu file.
COPY --from=alicecore . /opt/alicecore
RUN pip install --upgrade pip hatchling \
 && pip install /opt/alicecore

# ── Phụ thuộc bên thứ ba của API ────────────────────────────────────────────
# Lọc bỏ `alicecore` khỏi danh sách: nó vừa được cài từ nguồn cục bộ ở trên,
# để nguyên thì pip sẽ đi tìm trên PyPI và build hỏng.
COPY pyproject.toml ./
RUN python -c 'import os, tomllib; p=tomllib.load(open("pyproject.toml", "rb")); deps=list(p["project"]["dependencies"]); deps += p["project"]["optional-dependencies"].get(os.environ.get("SAG_API_EXTRAS", ""), []); deps=[d for d in deps if not d.split("[")[0].strip().lower().startswith("alicecore")]; print("\n".join(deps))' > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt

# Cài chính project này (không giải phụ thuộc, để rebuild nhanh).
COPY sag_api ./sag_api
COPY sag_agent ./sag_agent
RUN pip install --no-deps --no-build-isolation .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/v1/system/health || exit 1

CMD ["uvicorn", "sag_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
