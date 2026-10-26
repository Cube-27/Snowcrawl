import { Chip } from "@heroui/react";
import {
  CircleCheck,
  CircleDashed,
  CircleInfo,
  CircleXmark,
  TriangleExclamation,
} from "@gravity-ui/icons";
import type { ComponentProps, ComponentType, SVGProps } from "react";

/** What a status means, independent of the domain: each domain maps each of its status values to one tone, once. */
export type StatusTone = "neutral" | "accent" | "success" | "warning" | "danger";

// One icon and one HeroUI Chip color per tone, so a status never relies on color alone (WCAG 2.2 SC 1.4.1).
const TONES: Record<
  StatusTone,
  {
    icon: ComponentType<SVGProps<SVGSVGElement>>;
    color: ComponentProps<typeof Chip>["color"];
  }
> = {
  neutral: { icon: CircleDashed, color: "default" },
  accent: { icon: CircleInfo, color: "accent" },
  success: { icon: CircleCheck, color: "success" },
  warning: { icon: TriangleExclamation, color: "warning" },
  danger: { icon: CircleXmark, color: "danger" },
};

/** A status value shown the same way everywhere: its icon and its text in a HeroUI Chip. */
export function StatusLabel({ text, tone }: Readonly<{ text: string; tone: StatusTone }>) {
  const { icon: Icon, color } = TONES[tone];
  return (
    <Chip color={color}>
      <Icon aria-hidden="true" />
      <Chip.Label>{text}</Chip.Label>
    </Chip>
  );
}
