import { scaleLinear } from 'd3'

/** Negative red, neutral gray, positive green on the 0 to 10 scale. */
export const sentimentColor = scaleLinear<string>()
  .domain([0, 5, 10])
  .range(['#d8453b', '#9b9b9b', '#23965a'])
  .clamp(true)
