// expect: no-restricted-imports :: may be imported only under tests/**
import { attestReplayForTests } from "../src/api/trace/testing";
export const a = attestReplayForTests;
