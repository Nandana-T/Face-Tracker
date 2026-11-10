import "@/styles/globals.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { Providers } from "@/app/providers";
import { createAppRouter } from "@/app/router";
import { createQueryClient } from "@/lib/query-client";
import { useUiStore } from "@/lib/ui-store";

// Apply the persisted theme before first paint to avoid a flash.
document.documentElement.classList.toggle("dark", useUiStore.getState().theme === "dark");

const root = document.getElementById("root");
if (!root) throw new Error("Root element #root not found");

createRoot(root).render(
  <StrictMode>
    <Providers client={createQueryClient()}>
      <RouterProvider router={createAppRouter()} />
    </Providers>
  </StrictMode>,
);
