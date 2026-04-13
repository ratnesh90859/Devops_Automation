#!/bin/bash
set -e

PROJECT_ID="your-project-id"
REGION="asia-south1"
SERVICE="order-api"
REGISTRY="$REGION-docker.pkg.dev/$PROJECT_ID/infra-agent"

echo "--- Enabling APIs ---"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  logging.googleapis.com \
  --project=$PROJECT_ID

echo "--- Creating Artifact Registry ---"
gcloud artifacts repositories create infra-agent \
  --repository-format=docker \
  --location=$REGION \
  --project=$PROJECT_ID 2>/dev/null || true

echo "--- Building image ---"
cd infra-app
gcloud builds submit \
  --tag $REGISTRY/$SERVICE:latest \
  --project=$PROJECT_ID
cd ..

echo "--- Deploying to Cloud Run ---"
gcloud run deploy $SERVICE \
  --image $REGISTRY/$SERVICE:latest \
  --region $REGION \
  --project $PROJECT_ID \
  --memory 256Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 5 \
  --timeout 30 \
  --allow-unauthenticated

URL=$(gcloud run services describe $SERVICE \
  --region $REGION --project $PROJECT_ID \
  --format "value(status.url)")

echo ""
echo "=== Deployed: $URL ==="
echo "Add to .env → CLOUD_RUN_SERVICE_URL=$URL"
echo "Add to Bitbucket variables → CLOUD_RUN_SERVICE_URL=$URL"
