import { fmt } from "../utils/format";

export class UserService {
  lookup(id: string): string {
    return fmt(id);
  }
}

export function getUser(id: string): { id: string } {
  return { id: fmt(id) };
}
