import '@testing-library/jest-dom'
import { beforeAll, afterAll, afterEach } from 'vitest'
import { server } from './server'

// Use relative URLs for API calls during tests
process.env.VITE_API_BASE_URL = ''

beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())
