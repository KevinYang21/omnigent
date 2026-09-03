// Like useQuery but routes cache notifications through startTransition so
// re-renders are interruptible. Trade-offs vs useQuery:
// - No tearing protection (moves off useSyncExternalStore).
// - No Suspense / throwOnError support.
// - observer.setOptions called during render (not in effect like useBaseQuery)
//   so queryKey/enabled changes are urgent. Risk: setOptions can executeFetch
//   on an abandoned concurrent render — revisit on TQ upgrades.

import { useEffect, useState, useTransition } from "react";
import {
  QueryObserver,
  notifyManager,
  useQueryClient,
  type DefaultError,
  type QueryKey,
  type UseQueryOptions,
  type UseQueryResult,
} from "@tanstack/react-query";

export function useTransitionQuery<
  TQueryFnData = unknown,
  TError = DefaultError,
  TData = TQueryFnData,
  TQueryKey extends QueryKey = QueryKey,
>(options: UseQueryOptions<TQueryFnData, TError, TData, TQueryKey>): UseQueryResult<TData, TError> {
  const queryClient = useQueryClient();
  const defaultedOptions = queryClient.defaultQueryOptions(options);
  // eslint-disable-next-line no-underscore-dangle
  (defaultedOptions as { _optimisticResults?: string })._optimisticResults = "optimistic";

  const [observer] = useState(
    () =>
      new QueryObserver<TQueryFnData, TError, TData, TQueryFnData, TQueryKey>(
        queryClient,
        defaultedOptions,
      ),
  );

  // Sync options every render — keeps queryKey/enabled changes urgent.
  observer.setOptions(defaultedOptions);

  const [, startTransition] = useTransition();
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const unsubscribe = observer.subscribe(
      notifyManager.batchCalls(() => {
        startTransition(() => setTick((t) => t + 1));
      }),
    );
    // Close the render→effect gap: pick up any update that landed between
    // the render-phase getOptimisticResult and this subscribe call.
    observer.updateResult();
    return unsubscribe;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [observer]);

  void tick;
  // trackResult honours notifyOnChangeProps so only accessed fields trigger
  // re-renders — mirrors useBaseQuery's return.
  const result = observer.getOptimisticResult(defaultedOptions);
  return observer.trackResult(result) as UseQueryResult<TData, TError>;
}
