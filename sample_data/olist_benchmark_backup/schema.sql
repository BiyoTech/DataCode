--
-- PostgreSQL database dump
--


-- Dumped from database version 14.17
-- Dumped by pg_dump version 18.4 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: olist_benchmark_backup; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA olist_benchmark_backup;


SET default_table_access_method = heap;

--
-- Name: olist_customers_dataset; Type: TABLE; Schema: olist_benchmark_backup; Owner: -
--

CREATE TABLE olist_benchmark_backup.olist_customers_dataset (
    customer_id character varying(32),
    customer_unique_id character varying(32),
    customer_zip_code_prefix integer,
    customer_city character varying(100),
    customer_state character varying(50)
);


--
-- Name: olist_order_items_dataset; Type: TABLE; Schema: olist_benchmark_backup; Owner: -
--

CREATE TABLE olist_benchmark_backup.olist_order_items_dataset (
    order_id character varying(32),
    order_item_id integer,
    product_id character varying(32),
    seller_id character varying(32),
    shipping_limit_date timestamp without time zone,
    price numeric(10,2),
    freight_value numeric(10,2)
);


--
-- Name: olist_orders_dataset; Type: TABLE; Schema: olist_benchmark_backup; Owner: -
--

CREATE TABLE olist_benchmark_backup.olist_orders_dataset (
    order_id character varying(32),
    customer_id character varying(32),
    order_status character varying(20),
    order_purchase_timestamp timestamp without time zone,
    order_approved_at timestamp without time zone,
    order_delivered_carrier_date timestamp without time zone,
    order_delivered_customer_date timestamp without time zone,
    order_estimated_delivery_date timestamp without time zone
);


--
-- Name: olist_products_dataset; Type: TABLE; Schema: olist_benchmark_backup; Owner: -
--

CREATE TABLE olist_benchmark_backup.olist_products_dataset (
    product_id character varying(32),
    product_category_name character varying(100),
    product_name_lenght integer,
    product_description_lenght integer,
    product_photos_qty integer,
    product_weight_g integer,
    product_length_cm integer,
    product_height_cm integer,
    product_width_cm integer
);


--
-- Name: ads_order_detail_di; Type: VIEW; Schema: olist_benchmark_backup; Owner: -
--

CREATE VIEW olist_benchmark_backup.ads_order_detail_di AS
 SELECT o.order_id,
    o.customer_id,
    c.customer_unique_id,
    c.customer_zip_code_prefix,
    c.customer_city,
    c.customer_state,
    o.order_status,
    o.order_purchase_timestamp AS stat_date,
    o.order_approved_at,
    o.order_delivered_carrier_date,
    o.order_delivered_customer_date,
    o.order_estimated_delivery_date,
    i.order_item_id,
    i.product_id,
    i.seller_id,
    i.shipping_limit_date,
    i.price,
    i.freight_value,
    p.product_category_name AS product_category_en,
    p.product_name_lenght,
    p.product_description_lenght,
    p.product_photos_qty,
    p.product_weight_g,
    p.product_length_cm,
    p.product_height_cm,
    p.product_width_cm,
        CASE
            WHEN (o.order_delivered_customer_date IS NOT NULL) THEN 1
            ELSE 0
        END AS is_delivered,
        CASE
            WHEN (o.order_delivered_customer_date > o.order_estimated_delivery_date) THEN 1
            ELSE 0
        END AS is_delayed,
        CASE
            WHEN ((o.order_delivered_customer_date IS NOT NULL) AND (o.order_delivered_customer_date IS NOT NULL)) THEN (EXTRACT(epoch FROM (o.order_delivered_customer_date - o.order_purchase_timestamp)) / (86400)::numeric)
            ELSE NULL::numeric
        END AS delivery_days
   FROM (((olist_benchmark_backup.olist_orders_dataset o
     JOIN olist_benchmark_backup.olist_customers_dataset c ON (((o.customer_id)::text = (c.customer_id)::text)))
     JOIN olist_benchmark_backup.olist_order_items_dataset i ON (((o.order_id)::text = (i.order_id)::text)))
     LEFT JOIN olist_benchmark_backup.olist_products_dataset p ON (((i.product_id)::text = (p.product_id)::text)));


--
-- Name: olist_order_payments_dataset; Type: TABLE; Schema: olist_benchmark_backup; Owner: -
--

CREATE TABLE olist_benchmark_backup.olist_order_payments_dataset (
    order_id character varying(32),
    payment_sequential integer,
    payment_type character varying(20),
    payment_installments integer,
    payment_value numeric(10,2)
);


--
-- Name: ads_order_payment_di; Type: VIEW; Schema: olist_benchmark_backup; Owner: -
--

CREATE VIEW olist_benchmark_backup.ads_order_payment_di AS
 SELECT olist_order_payments_dataset.order_id,
    olist_order_payments_dataset.payment_sequential,
    olist_order_payments_dataset.payment_type,
    olist_order_payments_dataset.payment_installments,
    olist_order_payments_dataset.payment_value
   FROM olist_benchmark_backup.olist_order_payments_dataset;


--
-- Name: olist_order_reviews_dataset; Type: TABLE; Schema: olist_benchmark_backup; Owner: -
--

CREATE TABLE olist_benchmark_backup.olist_order_reviews_dataset (
    review_id character varying(32),
    order_id character varying(32),
    review_score integer,
    review_comment_title character varying(100),
    review_comment_message text,
    review_creation_date timestamp without time zone,
    review_answer_timestamp timestamp without time zone
);


--
-- Name: ads_order_review_di; Type: VIEW; Schema: olist_benchmark_backup; Owner: -
--

CREATE VIEW olist_benchmark_backup.ads_order_review_di AS
 SELECT r.review_id,
    r.order_id,
    r.review_score,
    r.review_comment_title,
    r.review_comment_message,
    r.review_creation_date AS review_creation_ts,
    r.review_answer_timestamp,
        CASE
            WHEN (r.review_score <= 2) THEN 1
            ELSE 0
        END AS is_negative,
        CASE
            WHEN ((r.review_answer_timestamp IS NOT NULL) AND (r.review_creation_date IS NOT NULL)) THEN (EXTRACT(epoch FROM (r.review_answer_timestamp - r.review_creation_date)) / (3600)::numeric)
            ELSE NULL::numeric
        END AS answer_hours,
    i.seller_id
   FROM (olist_benchmark_backup.olist_order_reviews_dataset r
     LEFT JOIN olist_benchmark_backup.olist_order_items_dataset i ON (((r.order_id)::text = (i.order_id)::text)));


--
-- Name: olist_geolocation_dataset; Type: TABLE; Schema: olist_benchmark_backup; Owner: -
--

CREATE TABLE olist_benchmark_backup.olist_geolocation_dataset (
    geolocation_zip_code_prefix integer,
    geolocation_lat numeric(10,7),
    geolocation_lng numeric(10,7),
    geolocation_city character varying(100),
    geolocation_state character varying(50)
);


--
-- Name: olist_sellers_dataset; Type: TABLE; Schema: olist_benchmark_backup; Owner: -
--

CREATE TABLE olist_benchmark_backup.olist_sellers_dataset (
    seller_id character varying(32),
    seller_zip_code_prefix integer,
    seller_city character varying(100),
    seller_state character varying(50)
);


--
-- Name: product_category_name_translation; Type: TABLE; Schema: olist_benchmark_backup; Owner: -
--

CREATE TABLE olist_benchmark_backup.product_category_name_translation (
    product_category_name character varying(100),
    product_category_name_english character varying(100)
);


--
-- PostgreSQL database dump complete
--


