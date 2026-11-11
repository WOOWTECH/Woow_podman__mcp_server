import { useQuery } from '@tanstack/react-query';
import { apiGet } from './api';

export function useCapabilities(enabled = true) {
  return useQuery({ queryKey: ['capabilities'], queryFn: () => apiGet('/capabilities'),
    enabled, staleTime: 30_000, retry: 1 });
}
