import { stopStack } from "./global-setup";

export default async function globalTeardown() {
  stopStack();
}
