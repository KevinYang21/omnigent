// Like useInfiniteQuery but routes cache notifications through startTransition.
// See useTransitionQuery.ts for trade-offs.

import { useEffect, useState, useTransition } from "react";
import {
  InfiniteQueryObserver,
  notifyManager,
  useQueryClient,
  type DefaultedInfiniteQueryObserverOptions,
  type DefaultError,
  type InfiniteData,
  type QueryKey,
  type UseInfiniteQueryOptions,
  type UseInfiniteQueryResult,
} from "@tanstack/react-query";

export function useTransitionInfiniteQuery<
  TQueryFnData,
  TError = DefaultError,
  TData = InfiniteData<TQueryFnData>,
  TQueryKey extends QueryKey = QueryKey,
  TPageParam = unknown,
>(
  options: UseInfiniteQueryOptions<TQueryFnData, TError, TData, TQueryKey, TPageParam>,
): UseInfiniteQueryResult<TData, TError> {
  const queryClient = useQueryClient();
  const defaultedOptions = queryClient.defaultQueryOptions(
    options,
  ) as DefaultedInfiniteQueryObserverOptions<TQueryFnData, TError, TData, TQueryKey, TPageParam>;
  // eslint-disable-next-line no-underscore-dangle
  (defaultedOptions as { _optimisticResults?: string })._optimisticResults = "optimistic";

  const [observer] = useState(
    () =>
      new InfiniteQueryObserver<TQueryFnData, TError, TData, TQueryKey, TPageParam>(
        queryClient,
        defaultedOptions,
      ),
  );

  observer.setOptions(defaultedOptions);

  const [, startTransition] = useTransition();
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const unsubscribe = observer.subscribe(
      notifyManager.batchCalls(() => {
        startTransition(() => setTick((t) => t + 1));
      }),
    );
    observer.updateResult();
    return unsubscribe;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [observer]);

  void tick;
  const result = observer.getOptimisticResult(defaultedOptions);
  return observer.trackResult(result) as UseInfiniteQueryResult<TData, TError>;
}
