# LIF Metadata Repository (MDR) Frontend

The **Metadata Repository (MDR)** is a key component of LIF. It provides the capabilities to maintain the *LIF data model* in all of its iterations---including an *organization-specific LIF data model* and *partner LIF data models.* The **MDR** and *LIF data model* will be maintained by the steward or organization governing LIF.

The **MDR** is a standalone component that serves as the LIF system of record. It is where individuals from implementing organizations maintain their *organization-specific LIF data model* and *partner LIF data models* through a graphical user interface.

Additionally, the **MDR** provides the capability for the organization to maintain source data model(s) and mappings to transform data into a structure aligned to the *organization-specific LIF data model*.

The **MDR** will enable the organization to define which elements of its *organization-specific LIF data model* can be shared externally as its *partner-accessible LIF data model*. As a Possible Future Roadmap Item, the **MDR** will also allow the retrieval of the *partner-accessible LIF data model* from partners that have allowed for queries via **LIF API**.

# Recommended Setup
```
cd deployments/advisor-demo-docker
docker compose down && docker compose build --no-cache && docker compose up -d
```
To rebuild this component of the stack specifically:
```
cd deployments/advisor-demo-docker
docker compose down lif-mdr-app && docker compose build lif-mdr-app --no-cache && docker compose up lif-mdr-app -d
```

# Setup Instructions
Prerequisites:
- Ensure you have `npm`, `docker`, and `docker-compose` installed.
- Ensure you have spun up the `lif_mdr_api` and `lif_mdr_database` services.

Ports are intentionally varied to validate changes.

Local / Docker Compose:
Start from the lif-main root directory
```
[[default is http://localhost:8012]]
export LIF_MDR_API_URL="http://localhost:8099"
docker-compose -f deployments/advisor-demo-docker/docker-compose.yml up --build
```

Local / Docker:
Start from the lif-main frontends/mdr-frontend directory
```
docker build --build-arg LIF_MDR_API_URL=http://localhost:8055 -t mdr-frontend:latest .
docker run -p 9000:80 mdr-frontend:latest
```

Local / Native:
Default MDR frontend URL is http://localhost:5173.
```
npm install
export VITE_API_URL=http://localhost:8099
npm run dev
```

# Deploying to Vercel

The app is a static Vite build, so it deploys to Vercel as-is. `vercel.json` in this directory carries the
build settings and the single-page-app rewrite that `nginx.conf` provides in the Docker image (every path
serves `index.html`, so client-side routes such as `/explore/data-mappings/38` and `/auth/callback` survive a
hard refresh). The Docker image and the S3 + CloudFront deploy in `.github/workflows/lif_mdr_frontend.yml`
are unchanged.

1. Import the repository into a Vercel project and set **Root Directory** to `frontends/mdr-frontend`
   (Vercel does not read a monorepo subdirectory otherwise). The framework, install, build and output
   settings come from `vercel.json`.
2. Set the build-time environment variables listed in `.env.example` under **Settings > Environment
   Variables**. Use `VITE_API_URL=/api` and `VITE_LDE_API_URL=/lde`: `vercel.json` rewrites those two path
   prefixes to the dev MDR API and dev LDE server-side, so the browser's requests stay same-origin and the dev
   API's CORS allow-list (which does not include the Vercel domain) never comes into play. Pointing
   `VITE_API_URL` straight at `https://mdr-api.dev.<domain>` fails the CORS preflight from a Vercel origin.
   To target another environment, change the two rewrite destinations. `VITE_LDE_API_URL` is needed only for
   the Export Playground page. Vite inlines these at build time, so redeploy after changing one.
3. Register the Vercel origin with Cognito, or leave `VITE_COGNITO_DOMAIN` and `VITE_COGNITO_CLIENT_ID`
   empty to fall back to the legacy username/password login. With Cognito on, the SPA app client must list
   `https://<your-vercel-domain>/auth/callback` as a callback URL and `https://<your-vercel-domain>/login` as
   a sign-out URL (`src/config/auth.ts` derives both from `window.location.origin`). Preview deployments get a
   new origin per deployment, so use a fixed production domain for Cognito or keep Cognito off on previews.
4. If you bypass the proxy and call an MDR API directly, that API must allow the Vercel origin: its
   `cors_allow_origins` setting defaults to `*`, but dev narrows it to its own frontend domain
   (`components/lif/mdr_utils/config.py`, `CORS_ALLOW_ORIGINS` in the ECS task definition).

To try the production build locally: `npm ci && npm run build`, then `npx vite preview`.
