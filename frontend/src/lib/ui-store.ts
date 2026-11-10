import { create } from "zustand";
import { persist } from "zustand/middleware";

export type Theme = "dark" | "light";

interface UiState {
  sidebarCollapsed: boolean;
  theme: Theme;
  toggleSidebar: () => void;
  setTheme: (theme: Theme) => void;
  reset: () => void;
}

const defaults = { sidebarCollapsed: false, theme: "dark" as Theme };

/** Global client state is limited to UI preferences shared by shell + settings (persisted). */
export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      ...defaults,
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setTheme: (theme) => set({ theme }),
      reset: () => set({ ...defaults }),
    }),
    {
      name: "katomaran-ui",
      partialize: (s) => ({ sidebarCollapsed: s.sidebarCollapsed, theme: s.theme }),
    },
  ),
);
