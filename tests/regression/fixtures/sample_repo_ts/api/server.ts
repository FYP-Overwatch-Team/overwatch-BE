import express from "express";
import { getUser } from "../services/user";

export function startServer(): void {
  const app = express();
  app.get("/user", (_req: unknown, res: { json(v: unknown): void }) => {
    res.json(getUser("1"));
  });
}
