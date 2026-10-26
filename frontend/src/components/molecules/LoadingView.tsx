import { Spinner } from "@heroui/react";

/** Data on its way: one spinner with a name, in place of the content it loads. */
export function LoadingView({ label = "Loading" }: Readonly<{ label?: string }>) {
  return (
    <div className="flex justify-center py-6">
      <Spinner aria-label={label} />
    </div>
  );
}
